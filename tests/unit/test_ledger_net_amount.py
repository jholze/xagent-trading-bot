"""#333: execution.amount is NET qty owned; filled_qty_gross is exchange filled."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.config import BotConfig
from core.models import OrderStatus, TradeOrder, TradeResult, execution_filled_qty_gross
from core.sim_ledger_replay import replay_simulated_ledger
from core.tenant_context import tenant_context
from data_manager import save_trade_history
from execution.gate_adapter import GateExecutionAdapter
from services.mcp.explain import sanitize_order
from services.order_service import OrderService
from services.portfolio_service import PortfolioService
from services.trading_service import TradingService
from strategies.positions import clear_positions_memory, get_key, get_position

SYMBOL = "COST/USDT"


def _cfg(*, fee_side_buy: str = "base") -> BotConfig:
    return BotConfig(
        {
            "trading_mode": "paper",
            "max_usdt_per_trade": 1000,
            "costs": {
                "fee_source": "config",
                "gate": {
                    "spot": {
                        "fee_maker_pct": 0.2,
                        "fee_taker_pct": 0.2,
                        "slippage_pct": 0.0,
                        "fee_side_buy": fee_side_buy,
                        "fee_side_sell": "quote",
                    }
                },
            },
            "live": {
                "execution": "shadow",
                "dry_run": False,
                "simulated_balance_usdt": 5000,
            },
        }
    )


def _mock_exchange(symbol: str = SYMBOL):
    ex = MagicMock(name="ccxt.gate")
    ex.amount_to_precision.side_effect = lambda _sym, amt: amt
    ex.load_markets.return_value = {
        symbol: {"limits": {"amount": {"min": 0.0}, "cost": {"min": 0.0}}}
    }
    return ex


def _isolate_ledger(monkeypatch):
    monkeypatch.setattr(
        "data_manager._reconcile_scoped_trade_history",
        lambda history, scope, config=None, **kwargs: (history, False),
    )
    monkeypatch.setattr(
        "data_manager._ledger_reads_mongo_trade_history", lambda *a, **k: False,
    )
    monkeypatch.setattr("data_manager._ledger_writes_mongo", lambda *a, **k: False)
    clear_positions_memory()
    save_trade_history(
        {"virtual_balance": 5000.0, "realized_pnl": 0.0, "open_positions": 0, "trades": []}
    )


def _shadow_adapter(monkeypatch, *, fee_side_buy: str = "base"):
    _isolate_ledger(monkeypatch)
    cfg = _cfg(fee_side_buy=fee_side_buy)
    monkeypatch.setattr("execution.gate_adapter.record_live_trade", lambda rec: None)
    adapter = GateExecutionAdapter(cfg, PortfolioService(cfg), mode="shadow")
    adapter._exchange = _mock_exchange(SYMBOL)
    adapter._fetch_usdt_balance = lambda: 5000.0
    return adapter


def _link_buy(result: TradeResult, order: TradeOrder, token: str) -> dict:
    with tenant_context("default", scope="paper"):
        svc = OrderService("paper")
        rec = svc.create_from_request(
            order,
            status=OrderStatus.QUEUED,
            telegram_token=token,
            timeframe="4h",
        )
        svc.link_execution_result(rec["id"], result, order)
        stored = svc.get_by_id(rec["id"])
    assert stored is not None
    return stored


def test_execution_filled_qty_gross_prefers_explicit_gross():
    assert execution_filled_qty_gross(
        {"amount": 9.98, "filled_qty_gross": 10.0}
    ) == pytest.approx(10.0)


def test_execution_filled_qty_gross_falls_back_to_amount_on_old_row():
    assert execution_filled_qty_gross({"amount": 10.0}) == pytest.approx(10.0)
    assert execution_filled_qty_gross(
        {}, {"amount": 10.0}
    ) == pytest.approx(10.0)


def test_link_execution_result_stores_net_amount_and_gross():
    order = TradeOrder("BUY", SYMBOL, 100.0, 10.0, usdt_amount=1000.0)
    result = TradeResult(
        True,
        "BUY",
        SYMBOL,
        amount=9.98,
        price=100.0,
        usdt_amount=1000.0,
        filled_qty=10.0,
        order_status=OrderStatus.EXECUTED,
    )
    stored = _link_buy(result, order, "n333-link")
    exe = stored["execution"]
    assert exe["amount"] == pytest.approx(9.98)
    assert exe["filled_qty_gross"] == pytest.approx(10.0)
    assert stored["filled_qty"] == pytest.approx(10.0)


def test_link_execution_result_omits_gross_when_filled_qty_unset():
    order = TradeOrder("BUY", SYMBOL, 100.0, 0, usdt_amount=1000.0)
    result = TradeResult(
        True, "BUY", SYMBOL, amount=2000, price=0.05, usdt_amount=100,
    )
    stored = _link_buy(result, order, "n333-nogross")
    assert "filled_qty_gross" not in (stored.get("execution") or {})
    assert stored["execution"]["amount"] == pytest.approx(2000)


def test_gross_reader_uses_filled_qty_gross_not_net_amount():
    exe = {"amount": 9.98, "filled_qty_gross": 10.0, "price": 100.0, "usdt": 1000.0}
    assert execution_filled_qty_gross(exe) == pytest.approx(10.0)
    replayed = TradingService._result_from_ledger(
        object(),
        {
            "status": "filled",
            "side": "buy",
            "symbol": SYMBOL,
            "id": "n333-replay",
            "execution": exe,
            "request": {},
            "pnl": 0,
        },
    )
    assert replayed.amount == pytest.approx(9.98)
    assert replayed.filled_qty == pytest.approx(10.0)
    sanitized = sanitize_order({"execution": exe, "request": {}, "risk": {}})
    assert sanitized["execution"]["filled_qty_gross"] == pytest.approx(10.0)
    assert sanitized["execution"]["amount"] == pytest.approx(9.98)


def test_old_row_without_filled_qty_gross_replays_amount_as_before():
    orders = [
        {
            "id": "old-gross-buy",
            "status": "filled",
            "side": "buy",
            "symbol": SYMBOL,
            "timeframe": "4h",
            "execution": {"price": 100.0, "amount": 10.0, "usdt": 1000.0},
            "timestamps": {"filled": "2026-01-01T00:00:00"},
        }
    ]
    replay = replay_simulated_ledger(orders, initial=5000.0)
    key = get_key(SYMBOL, "4h")
    assert replay["positions"][key]["amount"] == pytest.approx(10.0)


def test_buy_base_fee_replay_matches_position_book(monkeypatch):
    adapter = _shadow_adapter(monkeypatch, fee_side_buy="base")
    order = TradeOrder("BUY", SYMBOL, 100.0, 0, usdt_amount=1000.0)
    result = adapter.execute(order, "4h")
    assert result.executed, result.message
    assert result.filled_qty == pytest.approx(10.0)
    assert result.amount == pytest.approx(9.98)
    pos_qty = float(get_position(SYMBOL, "4h")["amount"])
    assert pos_qty == pytest.approx(9.98)

    stored = _link_buy(result, order, "n333-base")
    exe = stored["execution"]
    assert exe["amount"] == pytest.approx(9.98)
    assert exe["filled_qty_gross"] == pytest.approx(10.0)
    assert exe["amount"] != pytest.approx(exe["filled_qty_gross"])

    replay = replay_simulated_ledger([stored], initial=5000.0)
    key = get_key(SYMBOL, "4h")
    assert replay["positions"][key]["amount"] == pytest.approx(pos_qty)


def test_buy_quote_fee_gross_equals_net_and_replay_matches(monkeypatch):
    adapter = _shadow_adapter(monkeypatch, fee_side_buy="quote")
    order = TradeOrder("BUY", SYMBOL, 100.0, 0, usdt_amount=1000.0)
    result = adapter.execute(order, "4h")
    assert result.executed, result.message
    assert result.amount == pytest.approx(result.filled_qty)
    pos_qty = float(get_position(SYMBOL, "4h")["amount"])
    assert pos_qty == pytest.approx(result.amount)

    stored = _link_buy(result, order, "n333-quote")
    exe = stored["execution"]
    assert exe["amount"] == pytest.approx(exe["filled_qty_gross"])
    replay = replay_simulated_ledger([stored], initial=5000.0)
    key = get_key(SYMBOL, "4h")
    assert replay["positions"][key]["amount"] == pytest.approx(pos_qty)
