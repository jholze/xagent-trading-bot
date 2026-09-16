"""#466: Gate finish_as is optional venue metadata on closed/cancelled orders."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.config import BotConfig
from core.models import OrderStatus, TradeOrder, TradeResult
from execution.gate_adapter import GateExecutionAdapter
from execution.recovery import _apply_raw_without_adapter
from services.portfolio_service import PortfolioService

SOL = "SOL/USDT"
SYMBOL = "COST/USDT"


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
            "trading_mode": "paper",
            "max_usdt_per_trade": 2000,
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
        return_value=TradeResult(True, "BUY", SOL, amount=4.0, price=100, usdt_amount=400)
    )
    adapter.portfolio.execute_sell = MagicMock(
        return_value=TradeResult(True, "SELL", SOL, amount=4.0, price=100, usdt_amount=400)
    )
    return adapter, ex


def _shadow_adapter(monkeypatch):
    monkeypatch.setattr("execution.gate_adapter.record_live_trade", lambda rec: None)
    cfg = _cost_cfg()
    adapter = GateExecutionAdapter(cfg, PortfolioService(cfg), mode="shadow")
    ex = MagicMock(name="ccxt.gate")
    ex.amount_to_precision.side_effect = lambda _sym, amt: amt
    ex.cost_to_precision.side_effect = lambda _sym, amt: amt
    ex.load_markets.return_value = {
        SYMBOL: {"limits": {"amount": {"min": 0.0}, "cost": {"min": 0.0}}}
    }
    adapter._exchange = ex
    adapter._fetch_usdt_balance = lambda: 5000.0
    return adapter


def _raw(
    *,
    status: str,
    filled: float,
    average: float = 100.0,
    oid: str = "ex-1",
    finish_as: str | None = None,
    fee_ccy: str = "SOL",
) -> dict:
    payload = {
        "id": oid,
        "status": status,
        "filled": filled,
        "average": average,
        "cost": filled * average,
        "timestamp": 1_700_000_000_000,
        "fee": {"cost": filled * 0.002, "currency": fee_ccy},
    }
    if finish_as is not None:
        payload["info"] = {"finish_as": finish_as}
    return payload


def _buy(*, qty: float = 10.0, price: float = 100.0) -> TradeOrder:
    return TradeOrder("BUY", SOL, price, qty, usdt_amount=price * qty)


def _fill_qty(mock_execute_buy) -> float:
    kwargs = mock_execute_buy.call_args.kwargs
    fill = kwargs.get("fill")
    assert fill is not None
    return float(fill.qty_gross)


def test_closed_zero_fill_finish_as_cancelled_is_canceled(monkeypatch):
    adapter, ex = _real_adapter(monkeypatch)
    order = _buy()
    raw = _raw(status="closed", filled=0.0, finish_as="cancelled")
    result = adapter._finalize_exchange_order(
        ex, order, raw, side="buy", qty=10.0, timeframe="4h", usdt=1000.0
    )
    assert result.order_status is OrderStatus.CANCELED
    assert result.executed is False
    assert result.needs_reconcile is False
    assert result.pending is False
    assert order.status is OrderStatus.CANCELED
    adapter.portfolio.execute_buy.assert_not_called()


def test_closed_zero_fill_finish_as_canceled_american_is_canceled(monkeypatch):
    adapter, ex = _real_adapter(monkeypatch)
    raw = _raw(status="closed", filled=0.0, finish_as="CANCELED")
    result = adapter._finalize_exchange_order(
        ex, _buy(), raw, side="buy", qty=10.0, timeframe="4h", usdt=1000.0
    )
    assert result.order_status is OrderStatus.CANCELED
    assert result.executed is False
    assert result.needs_reconcile is False
    adapter.portfolio.execute_buy.assert_not_called()


def test_status_canceled_partial_fill_is_booked_then_canceled(monkeypatch):
    adapter, ex = _real_adapter(monkeypatch)
    order = _buy()
    raw = _raw(status="canceled", filled=4.0, average=100.0)
    result = adapter._finalize_exchange_order(
        ex, order, raw, side="buy", qty=10.0, timeframe="4h", usdt=1000.0
    )
    assert result.order_status is OrderStatus.CANCELED
    assert result.pending is False
    assert result.needs_reconcile is False
    assert order.status is OrderStatus.CANCELED
    assert result.filled_qty == pytest.approx(4.0)
    adapter.portfolio.execute_buy.assert_called_once()
    assert _fill_qty(adapter.portfolio.execute_buy) == pytest.approx(4.0)


def test_closed_finish_as_cancelled_partial_fill_is_booked_then_canceled(monkeypatch):
    adapter, ex = _real_adapter(monkeypatch)
    order = _buy()
    raw = _raw(status="closed", filled=4.0, average=100.0, finish_as="cancelled")
    result = adapter._finalize_exchange_order(
        ex, order, raw, side="buy", qty=10.0, timeframe="4h", usdt=1000.0
    )
    assert result.order_status is OrderStatus.CANCELED
    assert result.pending is False
    assert result.needs_reconcile is False
    assert order.status is OrderStatus.CANCELED
    assert result.filled_qty == pytest.approx(4.0)
    adapter.portfolio.execute_buy.assert_called_once()
    assert _fill_qty(adapter.portfolio.execute_buy) == pytest.approx(4.0)


def test_finish_as_open_with_fill_is_partially_filled(monkeypatch):
    adapter, ex = _real_adapter(monkeypatch)
    raw = _raw(status="closed", filled=4.0, average=100.0, finish_as="open")
    result = adapter._finalize_exchange_order(
        ex, _buy(), raw, side="buy", qty=10.0, timeframe="4h", usdt=1000.0
    )
    assert result.order_status is OrderStatus.PARTIALLY_FILLED
    assert result.pending is True
    assert result.executed is True
    assert result.filled_qty == pytest.approx(4.0)
    adapter.portfolio.execute_buy.assert_called_once()


def test_finish_as_filled_keeps_executed_path(monkeypatch):
    adapter, ex = _real_adapter(monkeypatch)
    raw = _raw(status="closed", filled=9.0, average=100.0, finish_as="filled")
    result = adapter._finalize_exchange_order(
        ex, _buy(), raw, side="buy", qty=10.0, timeframe="4h", usdt=1000.0
    )
    assert result.order_status is OrderStatus.EXECUTED
    assert result.executed is True
    assert result.pending is False
    adapter.portfolio.execute_buy.assert_called_once()


def test_synthesize_shadow_raw_has_no_finish_as(monkeypatch):
    adapter = _shadow_adapter(monkeypatch)
    order = TradeOrder("BUY", SYMBOL, 1.0, 0, usdt_amount=10.0)
    raw = adapter._synthesize_shadow_raw(order, side="buy", amount=10.0, usdt=10.0)
    assert "info" not in raw
    assert "finish_as" not in raw
    assert raw.get("status") == "closed"
    assert float(raw.get("filled") or 0) > 0


def test_finalize_shadow_shaped_closed_fill_without_info_is_executed(monkeypatch):
    adapter, ex = _real_adapter(monkeypatch)
    raw = {
        "id": "shadow-1",
        "status": "closed",
        "average": 100.0,
        "filled": 5.0,
        "cost": 500.0,
        "fee": {"cost": 0.01, "currency": "SOL"},
    }
    assert "info" not in raw
    assert "finish_as" not in raw
    order = TradeOrder("BUY", SOL, 100.0, 5.0, usdt_amount=500.0)
    result = adapter._finalize_exchange_order(
        ex, order, raw, side="buy", qty=5.0, timeframe="4h", usdt=500.0
    )
    assert result.executed is True
    assert result.order_status is OrderStatus.EXECUTED
    assert result.needs_reconcile is not True
    adapter.portfolio.execute_buy.assert_called_once()


def _recovery_pair(monkeypatch):
    adapter, _ex = _real_adapter(monkeypatch)
    cfg = adapter.config
    return adapter, cfg


def test_recovery_closed_zero_fill_finish_as_cancelled_is_canceled(monkeypatch):
    adapter, cfg = _recovery_pair(monkeypatch)
    order = _buy()
    raw = _raw(status="closed", filled=0.0, finish_as="cancelled")
    result = _apply_raw_without_adapter(
        raw, order, adapter=adapter, config=cfg, timeframe="4h"
    )
    assert result.order_status is OrderStatus.CANCELED
    assert result.executed is False
    assert result.needs_reconcile is False
    adapter.portfolio.execute_buy.assert_not_called()


def test_recovery_status_canceled_partial_fill_is_booked_then_canceled(monkeypatch):
    adapter, cfg = _recovery_pair(monkeypatch)
    order = _buy()
    raw = _raw(status="canceled", filled=4.0, average=100.0)
    result = _apply_raw_without_adapter(
        raw, order, adapter=adapter, config=cfg, timeframe="4h"
    )
    assert result.order_status is OrderStatus.CANCELED
    assert result.filled_qty == pytest.approx(4.0)
    assert result.needs_reconcile is False
    adapter.portfolio.execute_buy.assert_called_once()
    assert _fill_qty(adapter.portfolio.execute_buy) == pytest.approx(4.0)


def test_recovery_closed_finish_as_cancelled_partial_fill_is_booked_then_canceled(
    monkeypatch,
):
    adapter, cfg = _recovery_pair(monkeypatch)
    order = _buy()
    raw = _raw(status="closed", filled=4.0, average=100.0, finish_as="cancelled")
    result = _apply_raw_without_adapter(
        raw, order, adapter=adapter, config=cfg, timeframe="4h"
    )
    assert result.order_status is OrderStatus.CANCELED
    assert result.filled_qty == pytest.approx(4.0)
    assert result.needs_reconcile is False
    adapter.portfolio.execute_buy.assert_called_once()
    assert _fill_qty(adapter.portfolio.execute_buy) == pytest.approx(4.0)
