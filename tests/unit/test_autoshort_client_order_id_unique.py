"""Autoshort clientOrderId must stay unique within Gate's 26-byte payload.

The autoshort idempotency key is ``autoshort|{sym}|{tf}|{src}|{px:.8g}``
(services/trading_service.py). A plain 26-byte cut kept only
``autoshortSOMECOINUSDT1hora``: the source tail and the price were lost, so two
autoshorts on the same symbol and timeframe shared one clientOrderId and the
uncertain-create recovery could book an older order as the new one.
"""

from __future__ import annotations

import ccxt

from core.models import OrderStatus, TradeOrder
from execution.gate_adapter import (
    _GATE_TEXT_ALLOWED,
    _GATE_TEXT_PARAM_MAX_BYTES,
    _GATE_TEXT_PAYLOAD_MAX_BYTES,
    _GATE_TEXT_PREFIX,
    _clamp_gate_client_order_id,
)
from tests.unit.test_order_lifecycle import SYMBOL, _closed, _real_adapter


def _autoshort_key(sym: str, tf: str, src: str, px: float) -> str:
    # Same format as TradingService autoshort after a sell.
    return f"autoshort|{sym}|{tf}|{src}|{px:.8g}"


OLD = _autoshort_key("SOMECOIN/USDT", "1h", "oracle_climax", 0.012345)
NEW = _autoshort_key("SOMECOIN/USDT", "1h", "oracle_climax", 0.012399)


def _assert_valid_payload(payload: str) -> None:
    assert 0 < len(payload.encode("utf-8")) <= _GATE_TEXT_PAYLOAD_MAX_BYTES
    assert len(f"{_GATE_TEXT_PREFIX}{payload}".encode("utf-8")) <= _GATE_TEXT_PARAM_MAX_BYTES
    assert set(payload) <= _GATE_TEXT_ALLOWED


def test_two_autoshorts_differing_only_in_price_get_different_ids():
    a = _clamp_gate_client_order_id(OLD)
    b = _clamp_gate_client_order_id(NEW)
    _assert_valid_payload(a)
    _assert_valid_payload(b)
    assert a != b


def test_autoshorts_differing_only_in_source_tail_get_different_ids():
    a = _clamp_gate_client_order_id(_autoshort_key("SOMECOIN/USDT", "1h", "oracle_climax", 1.5))
    b = _clamp_gate_client_order_id(_autoshort_key("SOMECOIN/USDT", "1h", "oracle_cliff", 1.5))
    assert a != b


def test_many_autoshort_prices_do_not_collide():
    keys = [_autoshort_key("SOMECOIN/USDT", "1h", "oracle_climax", 0.01 + i * 1e-6) for i in range(2000)]
    payloads = {_clamp_gate_client_order_id(k) for k in keys}
    assert len(payloads) == len(keys)
    for p in payloads:
        _assert_valid_payload(p)


def test_clamp_is_deterministic_and_idempotent():
    first = _clamp_gate_client_order_id(NEW)
    assert _clamp_gate_client_order_id(NEW) == first
    # The persisted payload is clamped again before send: must be a no-op.
    assert _clamp_gate_client_order_id(first) == first


def test_payload_keeps_a_readable_head():
    assert _clamp_gate_client_order_id(NEW).startswith("autoshort")


def test_uuid_keys_keep_the_old_cut_for_in_flight_compat():
    key = "550e8400-e29b-41d4-a716-446655440000"
    assert _clamp_gate_client_order_id(key) == "550e8400e29b41d4a716446655"
    assert _clamp_gate_client_order_id("ab" * 20) == ("ab" * 20)[:_GATE_TEXT_PAYLOAD_MAX_BYTES]


def _short_order(key: str) -> TradeOrder:
    clamped = _clamp_gate_client_order_id(key)
    # TradingService stores the raw key as idempotency key and the clamped
    # payload as client_order_id before the adapter sees the order.
    return TradeOrder(
        "BUY",
        SYMBOL,
        100.0,
        0.0,
        usdt_amount=25.0,
        client_order_id=clamped,
        idempotency_key=key,
    )


def test_recovery_after_uncertain_create_ignores_older_autoshort(monkeypatch):
    adapter, ex, _ = _real_adapter(monkeypatch)
    older_id = _clamp_gate_client_order_id(OLD)
    older = _closed(
        filled=0.25,
        oid="ex-older",
        extra={"clientOrderId": f"{_GATE_TEXT_PREFIX}{older_id}", "text": f"{_GATE_TEXT_PREFIX}{older_id}"},
    )
    ex.create_market_buy_order_with_cost.side_effect = ccxt.RequestTimeout("timeout")
    ex.fetch_open_orders.return_value = [older]
    ex.fetch_order.side_effect = Exception("not found")

    result = adapter.execute(_short_order(NEW))

    assert result.exchange_order_id != "ex-older"
    assert result.executed is False
    assert result.order_status is OrderStatus.REJECTED
    adapter.portfolio.execute_buy.assert_not_called()
    assert ex.create_market_buy_order_with_cost.call_count == 1


def test_recovery_still_finds_the_same_autoshort(monkeypatch):
    adapter, ex, _ = _real_adapter(monkeypatch)
    own_id = _clamp_gate_client_order_id(NEW)
    own = _closed(filled=0.25, oid="ex-own", extra={"clientOrderId": f"{_GATE_TEXT_PREFIX}{own_id}"})
    ex.create_market_buy_order_with_cost.side_effect = ccxt.RequestTimeout("timeout")
    ex.fetch_open_orders.return_value = [own]

    result = adapter.execute(_short_order(NEW))

    assert result.executed
    assert result.exchange_order_id == "ex-own"
    assert ex.create_market_buy_order_with_cost.call_count == 1
