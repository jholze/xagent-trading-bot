"""GateExecutionAdapter shadow SHORT/COVER (#367). Fixture-only, no network."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.config import BotConfig
from core.costs import COST_MODEL_VERSION
from core.models import TradeOrder
from data_manager import save_trade_history
from execution.gate_adapter import GateExecutionAdapter
from services.portfolio_service import PortfolioService
from strategies.positions import clear_positions_memory, get_position, update_position
from strategies.short_math import is_short, margin_usdt, unrealized_pnl

SYMBOL = "SHDW/USDT"
REJECT_V0 = "shorts.allow_live=false — no Gate futures in v0"


@pytest.fixture(autouse=True)
def _reset_shadow_market_cache():
    GateExecutionAdapter._shadow_markets_cache = None
    GateExecutionAdapter._shadow_markets_failed = False
    GateExecutionAdapter._shadow_markets_warned = False
    yield
    GateExecutionAdapter._shadow_markets_cache = None
    GateExecutionAdapter._shadow_markets_failed = False
    GateExecutionAdapter._shadow_markets_warned = False


def _cost_cfg() -> BotConfig:
    return BotConfig(
        {
            "trading_mode": "live",
            "max_usdt_per_trade": 1000,
            "shorts": {
                "enabled": True,
                "allow_live": False,
                "leverage_default": 2,
                "leverage_cap": 2,
                "funding_rate_8h": 0.0,
            },
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
            "live": {
                "execution": "shadow",
                "dry_run": False,
                "simulated_balance_usdt": 5000,
            },
        }
    )


def _mock_exchange(symbol: str = SYMBOL, *, min_amount: float = 0.0, min_cost: float = 0.0):
    ex = MagicMock(name="ccxt.gate")
    ex.amount_to_precision.side_effect = lambda _sym, amt: amt
    ex.load_markets.return_value = {
        symbol: {
            "limits": {
                "amount": {"min": min_amount},
                "cost": {"min": min_cost},
            }
        }
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


def _adapter(monkeypatch, *, mode: str = "shadow", usdt_balance: float = 5000.0):
    _isolate_ledger(monkeypatch)
    cfg = _cost_cfg()
    captured: list[dict] = []
    monkeypatch.setattr(
        "execution.gate_adapter.record_live_trade",
        lambda rec: captured.append(rec),
    )
    adapter = GateExecutionAdapter(cfg, PortfolioService(cfg), mode=mode)
    adapter._exchange = _mock_exchange(SYMBOL)
    adapter._fetch_usdt_balance = lambda: usdt_balance
    return adapter, captured


def _short_order(*, leverage: float | None = 2.0, qty: float = 10, usdt: float = 1000.0, price: float = 100.0) -> TradeOrder:
    return TradeOrder(
        "SHORT",
        SYMBOL,
        price,
        qty,
        usdt_amount=usdt,
        leverage=leverage,
        signal="SHORT",
        source="manual",
    )


def _cover_order(*, qty: float = 10, price: float = 90.0) -> TradeOrder:
    return TradeOrder(
        "COVER",
        SYMBOL,
        price,
        qty,
        signal="COVER",
        source="manual",
        leverage=2,
    )


def _assert_exchange_untouched(ex):
    ex.create_order.assert_not_called()
    ex.create_market_buy_order.assert_not_called()
    ex.create_market_sell_order.assert_not_called()
    ex.set_leverage.assert_not_called()


def test_shadow_short_fills(monkeypatch):
    adapter, captured = _adapter(monkeypatch)
    order = _short_order()
    result = adapter.execute(order, "4h")
    assert result.executed, result.message
    assert str(result.exchange_order_id).startswith("shadow-")
    assert (result.message or "").startswith("shadow SHORT")
    _assert_exchange_untouched(adapter._exchange)

    pos = get_position(SYMBOL, "4h")
    assert is_short(pos)
    assert float(pos["amount"]) == pytest.approx(10.0)
    assert float(pos["leverage"]) == pytest.approx(2.0)

    assert captured
    rec = captured[-1]
    assert rec["type"] == "SHORT"
    assert rec["mode"] == "shadow"
    assert rec["leverage"] == pytest.approx(2.0)
    assert rec["margin_usdt"] == pytest.approx(
        margin_usdt(float(rec["amount"]), float(rec["price"]), 2)
    )
    assert rec["cost_model"] == COST_MODEL_VERSION
    assert "fee_base" in rec
    assert "fee_quote" in rec
    assert "fee_usdt" in rec


def test_shadow_cover_after_short_realizes_pnl(monkeypatch):
    adapter, captured = _adapter(monkeypatch)
    short = adapter.execute(_short_order(), "4h")
    assert short.executed, short.message
    pos = get_position(SYMBOL, "4h")
    entry = float(pos["average_entry"])
    qty = float(pos["amount"])

    result = adapter.execute(_cover_order(qty=qty, price=90.0), "4h")
    assert result.executed, result.message
    _assert_exchange_untouched(adapter._exchange)

    pos = get_position(SYMBOL, "4h")
    assert not (is_short(pos) and float(pos.get("amount") or 0) > 0)

    rec = captured[-1]
    assert rec["type"] == "COVER"
    assert rec["mode"] == "shadow"
    assert rec["pnl"] > 0
    assert "leverage" in rec
    assert "margin_usdt" in rec
    expected = unrealized_pnl("short", qty, entry, float(rec["price"]))
    assert rec["pnl"] == pytest.approx(expected, abs=1.0)


def test_shadow_cover_with_no_short_rejected(monkeypatch):
    adapter, captured = _adapter(monkeypatch)
    result = adapter.execute(_cover_order(), "4h")
    assert not result.executed
    assert result.message == "No short to cover"
    assert captured == []
    _assert_exchange_untouched(adapter._exchange)


def test_shadow_short_rejected_when_long_lot_open(monkeypatch):
    adapter, captured = _adapter(monkeypatch)
    update_position(SYMBOL, "4h", "BUY", 100.0, 5)
    result = adapter.execute(_short_order(), "4h")
    assert not result.executed
    assert "one-way" in (result.message or "")
    assert captured == []
    _assert_exchange_untouched(adapter._exchange)
    pos = get_position(SYMBOL, "4h")
    assert not is_short(pos)
    assert float(pos["amount"]) == pytest.approx(5.0)


def test_shadow_short_leverage_clamped_to_cap(monkeypatch):
    adapter, _captured = _adapter(monkeypatch)
    result = adapter.execute(_short_order(leverage=5), "4h")
    assert result.executed, result.message
    pos = get_position(SYMBOL, "4h")
    assert is_short(pos)
    assert float(pos["leverage"]) == pytest.approx(2.0)
    _assert_exchange_untouched(adapter._exchange)


def test_shadow_short_insufficient_margin_rejected(monkeypatch):
    adapter, captured = _adapter(monkeypatch, usdt_balance=1.0)
    result = adapter.execute(_short_order(), "4h")
    assert not result.executed
    assert "Insufficient USDT margin" in (result.message or "")
    assert captured == []
    _assert_exchange_untouched(adapter._exchange)
    pos = get_position(SYMBOL, "4h")
    assert not (is_short(pos) and float(pos.get("amount") or 0) > 0)


def test_real_mode_short_and_cover_rejected_frozen_message(monkeypatch):
    adapter, captured = _adapter(monkeypatch, mode="real")
    short = adapter.execute(_short_order(), "4h")
    assert not short.executed
    assert short.message == REJECT_V0
    cover = adapter.execute(_cover_order(), "4h")
    assert not cover.executed
    assert cover.message == REJECT_V0
    assert captured == []
    _assert_exchange_untouched(adapter._exchange)


def test_shadow_short_fills_when_load_markets_raises(monkeypatch):
    adapter, captured = _adapter(monkeypatch)
    adapter._exchange.load_markets.side_effect = RuntimeError("network down")
    adapter._exchange.load_markets.return_value = None
    GateExecutionAdapter._shadow_markets_cache = None
    GateExecutionAdapter._shadow_markets_failed = False
    GateExecutionAdapter._shadow_markets_warned = False

    result = adapter.execute(_short_order(), "4h")
    assert result.executed, result.message
    assert result.precision_unverified is True
    assert captured[-1]["precision_unverified"] is True
    assert str(result.exchange_order_id).startswith("shadow-")
    adapter._exchange.amount_to_precision.assert_not_called()
    _assert_exchange_untouched(adapter._exchange)
