"""#340: a closed exchange order is final even when filled < requested qty."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.config import BotConfig
from core.models import OrderStatus, TradeOrder, TradeResult
from core.tenant_context import tenant_context
from data_manager import load_trade_history, save_trade_history
from execution.gate_adapter import GateExecutionAdapter
from services.order_service import OrderService
from services.portfolio_service import PortfolioService
from strategies.positions import clear_positions_memory, get_position

SYMBOL = "COST/USDT"
SOL = "SOL/USDT"


@pytest.fixture(autouse=True)
def _reset_shadow_market_cache():
    GateExecutionAdapter._shadow_markets_cache = None
    GateExecutionAdapter._shadow_markets_failed = False
    GateExecutionAdapter._shadow_markets_warned = False
    yield
    GateExecutionAdapter._shadow_markets_cache = None
    GateExecutionAdapter._shadow_markets_failed = False
    GateExecutionAdapter._shadow_markets_warned = False


def _cost_cfg(*, slippage_pct: float = 0.0) -> BotConfig:
    return BotConfig(
        {
            "trading_mode": "paper",
            "max_usdt_per_trade": 2000,
            "costs": {
                "fee_source": "config",
                "gate": {
                    "spot": {
                        "fee_maker_pct": 0.2,
                        "fee_taker_pct": 0.2,
                        "slippage_pct": slippage_pct,
                        "fee_side_buy": "base",
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
    ex.cost_to_precision.side_effect = lambda _sym, amt: amt
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


def _shadow_adapter(monkeypatch, *, slippage_pct: float = 0.0):
    _isolate_ledger(monkeypatch)
    cfg = _cost_cfg(slippage_pct=slippage_pct)
    monkeypatch.setattr("execution.gate_adapter.record_live_trade", lambda rec: None)
    adapter = GateExecutionAdapter(cfg, PortfolioService(cfg), mode="shadow")
    adapter._exchange = _mock_exchange(SYMBOL)
    adapter._fetch_usdt_balance = lambda: 5000.0
    return adapter


def _real_adapter(monkeypatch):
    monkeypatch.setattr("execution.gate_adapter.record_live_trade", lambda rec: None)
    cfg = BotConfig(
        {
            "trading_mode": "live",
            "live_confirmed": True,
            "max_usdt_per_trade": 1000,
            "costs": {
                "fee_source": "config",
                "gate": {
                    "spot": {
                        "fee_maker_pct": 0.2,
                        "fee_taker_pct": 0.2,
                        "slippage_pct": 0.0,
                        "fee_side_buy": "base",
                        "fee_side_sell": "quote",
                    }
                },
            },
            "live": {"execution": "real", "dry_run": False, "max_usdt_per_trade": 1000},
        }
    )
    adapter = GateExecutionAdapter(cfg, PortfolioService(cfg), mode="real")
    ex = MagicMock(name="ccxt.gate")
    ex.amount_to_precision.side_effect = lambda _s, a: a
    ex.cost_to_precision.side_effect = lambda _s, a: a
    ex.load_markets.return_value = {
        SOL: {"limits": {"amount": {"min": 0}, "cost": {"min": 0}}}
    }
    adapter._exchange = ex
    adapter._fetch_usdt_balance = lambda: 10_000.0
    adapter.portfolio.execute_buy = MagicMock(
        return_value=TradeResult(True, "BUY", SOL, amount=9.0, price=111.11, usdt_amount=1000)
    )
    adapter.portfolio.execute_sell = MagicMock(
        return_value=TradeResult(True, "SELL", SOL, amount=9.0, price=100, usdt_amount=900)
    )
    return adapter, ex


def _closed(
    *,
    filled: float,
    requested_price: float = 100.0,
    average: float | None = None,
    oid: str = "ex-1",
    status: str = "closed",
    fee_ccy: str = "SOL",
) -> dict:
    avg = float(average if average is not None else requested_price)
    return {
        "id": oid,
        "status": status,
        "filled": filled,
        "average": avg,
        "cost": filled * avg,
        "timestamp": 1_700_000_000_000,
        "fee": {"cost": filled * 0.002, "currency": fee_ccy},
    }


def test_shadow_usdt_buy_worse_fill_is_executed_not_partial(monkeypatch):
    """Regression for #340: USDT market buy fills fewer coins than the request-price estimate."""
    adapter = _shadow_adapter(monkeypatch, slippage_pct=0.2)
    price = 0.008551
    usdt = 1520.0
    order = TradeOrder("BUY", SYMBOL, price, 0, usdt_amount=usdt)
    result = adapter.execute(order, "4h")

    requested = usdt / price
    assert result.filled_qty < requested
    assert result.executed is True, result.message
    assert result.pending is False
    assert result.order_status is OrderStatus.EXECUTED

    pos_qty = float(get_position(SYMBOL, "4h")["amount"])
    assert pos_qty == pytest.approx(result.amount)
    assert result.amount == pytest.approx(result.filled_qty * (1.0 - 0.002))

    history = load_trade_history()
    assert history["virtual_balance"] == pytest.approx(5000.0 - result.usdt_amount)
    assert result.usdt_amount == pytest.approx(usdt)

    with tenant_context("default", scope="paper"):
        svc = OrderService("paper")
        rec = svc.create_from_request(
            order, status=OrderStatus.QUEUED, telegram_token="n340-slip", timeframe="4h"
        )
        svc.link_execution_result(rec["id"], result, order)
        stored = svc.get_by_id(rec["id"])
    assert stored is not None
    assert stored["status"] == "filled"


def test_closed_raw_short_fill_is_executed(monkeypatch):
    adapter, ex = _real_adapter(monkeypatch)
    requested = 10.0
    filled = 9.0
    order = TradeOrder("BUY", SOL, 100.0, requested, usdt_amount=1000.0)
    raw = _closed(filled=filled, average=1000.0 / filled)
    result = adapter._finalize_exchange_order(
        ex, order, raw, side="buy", qty=requested, timeframe="4h", usdt=1000.0
    )
    assert result.executed is True, result.message
    assert result.pending is False
    assert result.order_status is OrderStatus.EXECUTED
    assert result.filled_qty == pytest.approx(filled)


def test_open_raw_partial_fill_stays_partial(monkeypatch):
    adapter, ex = _real_adapter(monkeypatch)
    requested = 10.0
    filled = 4.0
    order = TradeOrder("BUY", SOL, 100.0, requested, usdt_amount=1000.0)
    raw = _closed(filled=filled, average=100.0, status="open")
    result = adapter._finalize_exchange_order(
        ex, order, raw, side="buy", qty=requested, timeframe="4h", usdt=1000.0
    )
    assert result.order_status is OrderStatus.PARTIALLY_FILLED
    assert result.pending is True
    assert result.filled_qty == pytest.approx(filled)
    assert result.executed is True


def test_closed_raw_zero_fill_is_not_executed(monkeypatch):
    adapter, ex = _real_adapter(monkeypatch)
    requested = 10.0
    order = TradeOrder("BUY", SOL, 100.0, requested, usdt_amount=1000.0)
    raw = _closed(filled=0.0, average=100.0, status="closed")
    result = adapter._finalize_exchange_order(
        ex, order, raw, side="buy", qty=requested, timeframe="4h", usdt=1000.0
    )
    assert result.order_status is not OrderStatus.EXECUTED
    assert result.executed is False
    assert result.pending is True
    assert result.needs_reconcile is True
    adapter.portfolio.execute_buy.assert_not_called()
