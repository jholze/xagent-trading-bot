"""Order lifecycle (#313 §3.2/§3.3): mocked ccxt exchange, no live calls."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import ccxt
import pytest

from bus.trade_intents import make_idempotency_key
from core.config import BotConfig
from core.models import OrderStatus, TradeOrder, TradeResult
from execution.gate_adapter import GateExecutionAdapter
from services.portfolio_service import PortfolioService

SYMBOL = "SOL/USDT"


def _cfg() -> BotConfig:
    return BotConfig(
        {
            "trading_mode": "live",
            "live_confirmed": True,
            "max_usdt_per_trade": 100,
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
            "live": {"execution": "real", "dry_run": False, "max_usdt_per_trade": 100},
        }
    )


def _closed(
    *,
    filled: float,
    average: float = 100.0,
    oid: str = "ex-1",
    status: str = "closed",
    fee_ccy: str = "SOL",
    fee_cost: float = 0.0005,
    extra: dict | None = None,
) -> dict:
    raw = {
        "id": oid,
        "status": status,
        "filled": filled,
        "average": average,
        "cost": filled * average,
        "timestamp": 1_700_000_000_000,
        "fee": {"cost": fee_cost, "currency": fee_ccy},
        "clientOrderId": None,
    }
    if extra:
        raw.update(extra)
    return raw


def _real_adapter(monkeypatch):
    captured: list[dict] = []
    monkeypatch.setattr(
        "execution.gate_adapter.record_live_trade",
        lambda rec: captured.append(rec),
    )
    cfg = _cfg()
    adapter = GateExecutionAdapter(cfg, PortfolioService(cfg), mode="real")
    ex = MagicMock(name="ccxt.gate")
    ex.amount_to_precision.side_effect = lambda _s, a: a
    ex.cost_to_precision.side_effect = lambda _s, a: a
    ex.load_markets.return_value = {
        SYMBOL: {"limits": {"amount": {"min": 0}, "cost": {"min": 0}}}
    }
    ex.fetch_open_orders.return_value = []
    adapter._exchange = ex
    adapter._fetch_usdt_balance = lambda: 10_000.0
    adapter._fetch_base_balance = lambda _ex, _sym: 10_000.0
    adapter.portfolio.execute_buy = MagicMock(
        return_value=TradeResult(True, "BUY", SYMBOL, amount=0.25, price=100, usdt_amount=25)
    )
    adapter.portfolio.execute_sell = MagicMock(
        return_value=TradeResult(True, "SELL", SYMBOL, amount=0.25, price=100, usdt_amount=25)
    )
    return adapter, ex, captured


def _buy_order(*, key: str | None = None, usdt: float = 25.0, qty: float = 0.0) -> TradeOrder:
    key = key or str(uuid.uuid4())
    return TradeOrder(
        "BUY",
        SYMBOL,
        100.0,
        qty,
        usdt_amount=usdt,
        client_order_id=key,
        idempotency_key=key,
    )


def test_from_legacy_failed_is_rejected():
    assert OrderStatus.from_legacy("failed") is OrderStatus.REJECTED


def test_amount_is_qty_alias_and_remaining_qty():
    o = TradeOrder(type="SELL", symbol=SYMBOL, price=1.0, amount=10)
    assert o.qty == 10
    assert o.amount == 10
    o.filled_qty = 3
    assert o.remaining_qty == 7
    o.amount = 12
    assert o.qty == 12


def test_full_fill(monkeypatch):
    adapter, ex, _ = _real_adapter(monkeypatch)
    ex.create_market_buy_order_with_cost.return_value = _closed(filled=0.25)
    result = adapter.execute(_buy_order())
    assert result.executed
    assert result.pending is False
    assert result.order_status is OrderStatus.EXECUTED
    assert result.filled_qty == pytest.approx(0.25)
    assert result.order_exist_in_exchange is True
    adapter.portfolio.execute_buy.assert_called_once()
    fill = adapter.portfolio.execute_buy.call_args.kwargs.get("fill")
    assert fill is not None
    assert fill.qty_gross == pytest.approx(0.25)


def test_partial_fill(monkeypatch):
    adapter, ex, _ = _real_adapter(monkeypatch)
    order = _buy_order(qty=0.25)
    ex.create_market_buy_order_with_cost.return_value = _closed(filled=0.10, status="open")
    result = adapter.execute(order)
    assert result.executed
    assert result.order_status is OrderStatus.PARTIALLY_FILLED
    assert result.filled_qty == pytest.approx(0.10)
    assert result.pending is True
    fill = adapter.portfolio.execute_buy.call_args.kwargs.get("fill")
    assert fill.qty_gross == pytest.approx(0.10)


def test_filled_missing_then_fetch_order_resolves(monkeypatch):
    adapter, ex, _ = _real_adapter(monkeypatch)
    created = _closed(filled=0.25)
    created.pop("filled")
    ex.create_market_buy_order_with_cost.return_value = created
    ex.fetch_order.return_value = _closed(filled=0.25, oid="ex-1")
    result = adapter.execute(_buy_order())
    assert result.executed
    assert result.order_status is OrderStatus.EXECUTED
    ex.fetch_order.assert_called_once()


def test_filled_missing_twice_active_pending(monkeypatch):
    adapter, ex, _ = _real_adapter(monkeypatch)
    created = {"id": "ex-1", "status": "closed", "average": 100.0, "fee": {"cost": 0, "currency": "SOL"}}
    ex.create_market_buy_order_with_cost.return_value = created
    ex.fetch_order.return_value = dict(created)
    result = adapter.execute(_buy_order())
    assert result.executed is False
    assert result.pending is True
    assert result.order_status is OrderStatus.ACTIVE
    assert result.needs_reconcile is True
    adapter.portfolio.execute_buy.assert_not_called()
    assert ex.fetch_order.call_count == 1


def test_timeout_then_found_via_fetch_open_orders(monkeypatch):
    adapter, ex, _ = _real_adapter(monkeypatch)
    key = str(uuid.uuid4())
    ex.create_market_buy_order_with_cost.side_effect = ccxt.RequestTimeout("timeout")
    found = _closed(filled=0.25, oid="ex-found", extra={"clientOrderId": key})
    ex.fetch_open_orders.return_value = [found]
    result = adapter.execute(_buy_order(key=key))
    assert result.executed
    assert result.order_status is OrderStatus.EXECUTED
    assert result.exchange_order_id == "ex-found"
    ex.fetch_open_orders.assert_called()
    adapter.portfolio.execute_buy.assert_called_once()


def test_timeout_then_not_found_rejected(monkeypatch):
    adapter, ex, _ = _real_adapter(monkeypatch)
    ex.create_market_buy_order_with_cost.side_effect = ccxt.RequestTimeout("timeout")
    ex.fetch_open_orders.return_value = []
    ex.fetch_order.side_effect = Exception("not found")
    result = adapter.execute(_buy_order())
    assert result.executed is False
    assert result.order_status is OrderStatus.REJECTED
    assert "not placed" in (result.message or "")
    adapter.portfolio.execute_buy.assert_not_called()
    # never resent
    assert ex.create_market_buy_order_with_cost.call_count == 1


def test_insufficient_funds_rejected(monkeypatch):
    adapter, ex, _ = _real_adapter(monkeypatch)
    ex.create_market_buy_order_with_cost.side_effect = ccxt.InsufficientFunds("no usdt")
    result = adapter.execute(_buy_order())
    assert result.executed is False
    assert result.order_status is OrderStatus.REJECTED
    assert "no usdt" in (result.message or "")
    adapter.portfolio.execute_buy.assert_not_called()


def test_exchange_canceled_no_position_change(monkeypatch):
    adapter, ex, _ = _real_adapter(monkeypatch)
    ex.create_market_buy_order_with_cost.return_value = _closed(
        filled=0.0, status="canceled"
    )
    result = adapter.execute(_buy_order())
    assert result.executed is False
    assert result.order_status is OrderStatus.CANCELED
    adapter.portfolio.execute_buy.assert_not_called()
    adapter.portfolio.execute_sell.assert_not_called()


def test_unknown_fee_currency_records_fill(monkeypatch):
    adapter, ex, _ = _real_adapter(monkeypatch)
    logged: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "execution.gate_adapter.log",
        lambda msg, level="INFO": logged.append((str(msg), str(level))),
    )
    ex.create_market_buy_order_with_cost.return_value = _closed(
        filled=0.25, fee_ccy="DOGE", fee_cost=1.0
    )
    result = adapter.execute(_buy_order())
    assert result.executed
    assert result.fee_unknown is True
    assert result.needs_reconcile is True
    adapter.portfolio.execute_buy.assert_called_once()
    assert any(lvl == "ERROR" and "Unknown fee" in msg for msg, lvl in logged)


def test_buy_uses_create_market_buy_order_with_cost_and_text_uuid(monkeypatch):
    adapter, ex, _ = _real_adapter(monkeypatch)
    key = str(uuid.uuid4())
    ex.create_market_buy_order_with_cost.return_value = _closed(filled=0.25)
    result = adapter.execute(_buy_order(key=key, usdt=25.0))
    assert result.executed
    ex.create_market_buy_order.assert_not_called()
    ex.create_market_buy_order_with_cost.assert_called_once()
    args, kwargs = ex.create_market_buy_order_with_cost.call_args
    assert args[0] == SYMBOL
    assert float(args[1]) == pytest.approx(25.0)
    params = args[2] if len(args) > 2 else kwargs.get("params") or {}
    assert params.get("text") == f"t-{key}"
    uuid.UUID(str(params["text"])[2:])
    assert "createMarketBuyOrderRequiresPrice" not in params


def test_idempotency_key_is_uuid_and_stable_across_retry(monkeypatch):
    a = make_idempotency_key("SOL/USDT", "4h", "BUY", "auto", "paper")
    b = make_idempotency_key("SOL/USDT", "4h", "BUY", "auto", "paper", bucket="2026010112")
    uuid.UUID(a)
    uuid.UUID(b)
    assert a != b  # mint is unique; stability is reuse of the stored key

    adapter, ex, _ = _real_adapter(monkeypatch)
    key = str(uuid.uuid4())
    ex.create_market_buy_order_with_cost.return_value = _closed(filled=0.25)
    order = _buy_order(key=key)
    adapter.execute(order)
    adapter.execute(order)
    texts = []
    for call in ex.create_market_buy_order_with_cost.call_args_list:
        args, kwargs = call
        params = args[2] if len(args) > 2 else kwargs.get("params") or {}
        texts.append(params.get("text"))
    assert texts == [f"t-{key}", f"t-{key}"]
