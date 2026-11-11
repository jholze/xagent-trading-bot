"""Autoshort clientOrderId must stay unique within Gate's 26-byte payload.

The autoshort idempotency key is ``autoshort|{sym}|{tf}|{src}|{px:.8g}``
(services/trading_service.py). A plain 26-byte cut kept only
``autoshortSOMECOINUSDT1hora``: the source tail and the price were lost, so two
autoshorts on the same symbol and timeframe shared one clientOrderId and the
uncertain-create recovery could book an older order as the new one.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import ccxt

from core.models import OrderStatus, TradeOrder, TradeResult
from execution.gate_adapter import (
    _GATE_TEXT_ALLOWED,
    _GATE_TEXT_PARAM_MAX_BYTES,
    _GATE_TEXT_PAYLOAD_MAX_BYTES,
    _GATE_TEXT_PREFIX,
    _clamp_gate_client_order_id,
    _legacy_gate_client_order_id,
)
from execution.recovery import _fetch_order_raw, _order_ids, _resolve_in_flight_orders
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


def test_recovery_looks_up_pre_deploy_26_char_id_and_current_clamp():
    """#763: a raw key from before the hash clamp must still be a lookup id."""
    legacy = _legacy_gate_client_order_id(OLD)
    current = _clamp_gate_client_order_id(OLD)
    assert legacy != current
    assert len(legacy.encode("utf-8")) == _GATE_TEXT_PAYLOAD_MAX_BYTES
    assert set(legacy) <= _GATE_TEXT_ALLOWED
    assert "." not in legacy
    ids = _order_ids({"client_order_id": OLD, "idempotency_key": OLD, "status": "active"})
    assert OLD in ids
    assert current in ids
    assert legacy in ids
    assert ids.count(current) == 1
    assert ids.count(legacy) == 1


def test_recovery_adopts_one_id_and_does_not_resend(monkeypatch):
    """#763: both id forms are tried; the first hit is the only match.

    The other form is not adopted after that hit, and startup recovery does
    not call create_order.
    """
    legacy = _legacy_gate_client_order_id(OLD)
    current = _clamp_gate_client_order_id(OLD)
    current_raw = _closed(
        filled=0.25,
        oid="ex-current",
        extra={"clientOrderId": f"{_GATE_TEXT_PREFIX}{current}"},
    )
    legacy_raw = _closed(
        filled=0.25,
        oid="ex-legacy",
        extra={"side": "sell", "clientOrderId": f"{_GATE_TEXT_PREFIX}{legacy}"},
    )
    record = {
        "id": "local-1",
        "symbol": "SOMECOIN/USDT",
        "status": "active",
        "side": "sell",
        "qty": 0.25,
        "timeframe": "1h",
        "client_order_id": OLD,
        "idempotency_key": OLD,
        "request": {"price": 0.012345, "amount": 0.25},
    }

    def _run(hit: str):
        fetched: list[str] = []

        def fetch_order(ident, *_args, **_kwargs):
            fetched.append(str(ident))
            if hit == "current" and ident == current:
                return current_raw
            if hit == "legacy" and ident == legacy:
                return legacy_raw
            raise RuntimeError(f"miss {ident}")

        ex = MagicMock()
        ex.fetch_order.side_effect = fetch_order
        ex.create_order.side_effect = AssertionError("recovery resent an order")
        raw = _fetch_order_raw(ex, record)
        return ex, fetched, raw

    ex, fetched, raw = _run("current")
    assert raw["id"] == "ex-current"
    assert current in fetched
    assert legacy not in fetched
    assert ex.create_order.call_count == 0

    ex, fetched, raw = _run("legacy")
    assert raw["id"] == "ex-legacy"
    assert current in fetched
    assert legacy in fetched
    assert fetched.index(legacy) > fetched.index(current)
    assert ex.create_order.call_count == 0

    class GateExecutionAdapter:
        def __init__(self) -> None:
            self.finalized = 0

        def _finalize_exchange_order(self, *_a, **_k):
            self.finalized += 1
            return TradeResult(
                True,
                "SELL",
                "SOMECOIN/USDT",
                amount=0.25,
                price=0.012345,
                order_status=OrderStatus.EXECUTED,
            )

    ex, _fetched, _raw = _run("legacy")
    adapter = GateExecutionAdapter()
    monkeypatch.setattr("execution.recovery._link_result", lambda *_a, **_k: None)
    resolved = _resolve_in_flight_orders(
        [record],
        exchange=ex,
        adapter=adapter,
        config=None,
        scope="demo",
    )
    assert adapter.finalized == 1
    assert len(resolved) == 1
    assert ex.create_order.call_count == 0


def test_legacy_short_hyphenated_key_keeps_the_hyphen():
    """Pre-#753 removed hyphens only after the 26-byte cut was required."""
    assert _legacy_gate_client_order_id("abc-key") == "abc-key"
    assert _clamp_gate_client_order_id("abc-key") == "abc-key"


def test_head_digest_id_keeps_a_dot_inside_the_eleven_char_head():
    from execution.gate_adapter import _GATE_CLIENT_ID_DIGEST_CHARS, _GATE_CLIENT_ID_HEAD_CHARS
    from execution.recovery import _is_head_digest_client_id

    digest = "a" * _GATE_CLIENT_ID_DIGEST_CHARS
    head = ("ab." + ("c" * _GATE_CLIENT_ID_HEAD_CHARS))[:_GATE_CLIENT_ID_HEAD_CHARS]
    assert len(head) == _GATE_CLIENT_ID_HEAD_CHARS and "." in head
    assert _is_head_digest_client_id(f"{head}.{digest}") is True
    assert _is_head_digest_client_id(f"{head}.{'z' * _GATE_CLIENT_ID_DIGEST_CHARS}") is False


def _pre753_record(**over):
    record = {
        "id": "local-pre",
        "symbol": "SOMECOIN/USDT",
        "status": "active",
        "side": "sell",
        "qty": 0.25,
        "timeframe": "1h",
        "client_order_id": OLD,
        "idempotency_key": OLD,
        "timestamps": {"created": "2026-11-01T00:00:00+00:00"},
        "request": {"price": 0.012345, "amount": 0.25, "usdt": 25.0},
    }
    record.update(over)
    return record


def _legacy_fetch(record, raw, *, market=None):
    legacy = _legacy_gate_client_order_id(str(record.get("idempotency_key") or OLD))

    def fetch_order(ident, *_args, **_kwargs):
        if ident == legacy:
            return raw
        raise RuntimeError(f"miss {ident}")

    ex = MagicMock()
    ex.fetch_order.side_effect = fetch_order
    ex.create_order.side_effect = AssertionError("recovery resent an order")
    if market is None:
        ex.market.side_effect = Exception("spot")
    else:
        ex.market.return_value = market
    return ex, _fetch_order_raw(ex, record)


def test_legacy_hit_with_wrong_side_size_or_age_is_not_adopted():
    """A skipped or always-true match check must not adopt a foreign legacy hit."""
    legacy = _legacy_gate_client_order_id(OLD)
    good = {
        "side": "sell",
        "amount": 0.25,
        "filled": 0.25,
        "cost": 0.003,
        "clientOrderId": f"{_GATE_TEXT_PREFIX}{legacy}",
        "timestamp": 1_762_000_000_000,
    }
    cases = [
        {**good, "side": "buy"},
        {**good, "amount": 9.0, "filled": 9.0, "cost": 90.0},
        {**good, "timestamp": 1_600_000_000_000},
    ]
    for extra in cases:
        record = _pre753_record()
        raw = _closed(filled=extra["filled"], oid="ex-foreign", extra=extra)
        _ex, found = _legacy_fetch(record, raw)
        assert found is None, extra


def test_legacy_spot_market_buy_matches_quote_amount_not_base_qty():
    """Spot market buy ``amount`` is quote USDT, not base qty."""
    record = _pre753_record(
        side="buy",
        qty=0.25,
        request={"price": 100.0, "amount": 0.25, "usdt": 25.0},
    )
    legacy = _legacy_gate_client_order_id(OLD)
    raw = _closed(
        filled=0.0,
        oid="ex-buy",
        extra={
            "side": "buy",
            "amount": 25.0,
            "filled": None,
            "cost": 25.0,
            "clientOrderId": f"{_GATE_TEXT_PREFIX}{legacy}",
            "timestamp": 1_799_500_000_000,
        },
    )
    _ex, found = _legacy_fetch(record, raw)
    assert found is not None
    assert found["id"] == "ex-buy"
    assert found["amount"] != record["qty"]


def test_legacy_swap_short_matches_contracts_times_contract_size():
    record = _pre753_record(
        symbol="SOMECOIN/USDT:USDT",
        side="short",
        qty=20.0,
        request={"price": 1.0, "amount": 20.0},
    )
    legacy = _legacy_gate_client_order_id(OLD)
    raw = _closed(
        filled=2.0,
        oid="ex-swap",
        extra={
            "side": "sell",
            "amount": 2.0,
            "filled": 2.0,
            "cost": 20.0,
            "clientOrderId": f"{_GATE_TEXT_PREFIX}{legacy}",
            "timestamp": 1_799_500_000_000,
        },
    )
    _ex, found = _legacy_fetch(record, raw, market={"contractSize": 10})
    assert found is not None
    assert found["id"] == "ex-swap"
    assert found["amount"] != record["qty"]


def test_post_753_row_does_not_adopt_a_legacy_cut_from_another_key(monkeypatch):
    """#753 collision: the shared 26-character cut must not finalize a different order.

    A post-#753 row stores the current clamp. The current id misses. The legacy
    cut would return an older order from a different key. That order is not
    finalized and the row stays ACTIVE.
    """
    other = _autoshort_key("SOMECOIN/USDT", "1h", "oracle_cliff", 0.5)
    legacy = _legacy_gate_client_order_id(OLD)
    current = _clamp_gate_client_order_id(OLD)
    assert legacy == _legacy_gate_client_order_id(other)
    assert current != _clamp_gate_client_order_id(other)
    record = {
        "id": "local-post-753",
        "symbol": "SOMECOIN/USDT",
        "status": "active",
        "side": "sell",
        "qty": 0.25,
        "timeframe": "1h",
        "client_order_id": current,
        "idempotency_key": OLD,
        "timestamps": {"created": "2026-11-01T00:00:00+00:00"},
        "request": {"price": 0.012345, "amount": 0.25},
    }
    assert legacy not in _order_ids(record)
    foreign = _closed(
        filled=9.0,
        oid="ex-other-key",
        extra={
            "side": "sell",
            "clientOrderId": f"{_GATE_TEXT_PREFIX}{legacy}",
            "timestamp": 1_600_000_000_000,
        },
    )

    def fetch_order(ident, *_args, **_kwargs):
        if ident == legacy:
            return foreign
        raise RuntimeError(f"miss {ident}")

    ex = MagicMock()
    ex.fetch_order.side_effect = fetch_order
    ex.create_order.side_effect = AssertionError("recovery resent an order")
    assert _fetch_order_raw(ex, record) is None

    class GateExecutionAdapter:
        def __init__(self) -> None:
            self.finalized = 0

        def _finalize_exchange_order(self, *_a, **_k):
            self.finalized += 1
            raise AssertionError("legacy collision was finalized")

    monkeypatch.setattr("execution.recovery._link_result", lambda *_a, **_k: None)
    resolved = _resolve_in_flight_orders(
        [record],
        exchange=ex,
        adapter=GateExecutionAdapter(),
        config=None,
        scope="demo",
    )
    assert resolved == []
    assert record["status"] == "active"
    assert ex.create_order.call_count == 0


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
