"""#439: Gate clientOrderId / text payload is ≤28 bytes and charset-legal."""

from __future__ import annotations

import re
from unittest.mock import MagicMock

from core.config import BotConfig
from core.models import RiskDecision, TradeOrder, TradeResult
from core.tenant_context import tenant_context
from execution.gate_adapter import (
    GateExecutionAdapter,
    _GATE_TEXT_ALLOWED,
    _GATE_TEXT_PARAM_MAX_BYTES,
    _GATE_TEXT_PAYLOAD_MAX_BYTES,
    _GATE_TEXT_PREFIX,
    _clamp_gate_client_order_id,
)
from execution.recovery import _order_ids
from services.order_service import OrderService
from services.portfolio_service import PortfolioService
from services.trading_service import TradingService

SYMBOL = "SOL/USDT"
_CHARSET = re.compile(r"^[0-9A-Za-z_.-]+$")
# Canonical uuid4 (36 chars with hyphens). Clamp: strip hyphens, then cap.
_UUID4 = "550e8400-e29b-41d4-a716-446655440000"
_UUID4_CLAMPED = "550e8400e29b41d4a716446655"  # 26 hex; t- + payload ≤ 28


def _adapter() -> GateExecutionAdapter:
    return GateExecutionAdapter(BotConfig({}), MagicMock(), mode="shadow")


def _order(*, client_order_id: str = "", idempotency_key: str = "") -> TradeOrder:
    return TradeOrder(
        "BUY",
        SYMBOL,
        100.0,
        1.0,
        usdt_amount=100.0,
        client_order_id=client_order_id,
        idempotency_key=idempotency_key,
    )


def _assert_gate_text(params: dict, order: TradeOrder) -> str:
    text = str(params.get("text") or "")
    assert text.startswith(_GATE_TEXT_PREFIX)
    payload = text[len(_GATE_TEXT_PREFIX) :]
    assert len(payload.encode("utf-8")) <= 28
    # ccxt 4.5.48 checks len(params.text) before adding t- if missing.
    assert len(text.encode("utf-8")) <= _GATE_TEXT_PARAM_MAX_BYTES
    assert _CHARSET.fullmatch(payload)
    assert set(payload) <= _GATE_TEXT_ALLOWED
    assert order.client_order_id == payload
    return payload


def test_uuid4_client_order_id_is_clamped_and_persisted():
    adapter = _adapter()
    order = _order(client_order_id=_UUID4, idempotency_key=_UUID4)
    params = adapter._client_order_params(order)
    payload = _assert_gate_text(params, order)
    assert payload == _UUID4_CLAMPED
    assert params["text"] == f"{_GATE_TEXT_PREFIX}{_UUID4_CLAMPED}"
    # Non-empty idempotency_key is left as the caller's uuid4.
    assert order.idempotency_key == _UUID4


def test_uuid4_idempotency_key_only_is_clamped():
    adapter = _adapter()
    order = _order(idempotency_key=_UUID4)
    # TradeOrder copies idempotency_key onto client_order_id when empty.
    assert order.client_order_id == _UUID4
    params = adapter._client_order_params(order)
    payload = _assert_gate_text(params, order)
    assert payload == _UUID4_CLAMPED


def test_fresh_mint_is_uuid16(monkeypatch):
    class _Fixed:
        hex = "1234567890abcdef1234567890abcdef"

        def __str__(self):
            return "12345678-90ab-cdef-1234-567890abcdef"

    monkeypatch.setattr(
        "execution.gate_adapter.uuid.uuid4", lambda: _Fixed()
    )
    adapter = _adapter()
    order = _order()
    assert order.client_order_id == ""
    assert order.idempotency_key == ""
    params = adapter._client_order_params(order)
    payload = _assert_gate_text(params, order)
    assert payload == "1234567890abcdef"
    assert len(payload) == 16
    assert order.idempotency_key == payload


def test_fresh_mint_is_at_most_28_without_patch():
    adapter = _adapter()
    order = _order()
    params = adapter._client_order_params(order)
    payload = _assert_gate_text(params, order)
    assert len(payload.encode("utf-8")) <= 28
    assert order.idempotency_key == payload


def test_second_call_with_clamped_key_is_noop():
    adapter = _adapter()
    order = _order(client_order_id=_UUID4, idempotency_key=_UUID4)
    first = adapter._client_order_params(order)
    assert order.client_order_id == _UUID4_CLAMPED
    second = adapter._client_order_params(order)
    assert first == second
    assert first["text"] == f"{_GATE_TEXT_PREFIX}{_UUID4_CLAMPED}"
    assert order.client_order_id == _UUID4_CLAMPED


def test_retry_of_original_uuid4_is_deterministic():
    adapter = _adapter()
    a = _order(client_order_id=_UUID4, idempotency_key=_UUID4)
    b = _order(client_order_id=_UUID4, idempotency_key=_UUID4)
    pa = adapter._client_order_params(a)
    pb = adapter._client_order_params(b)
    assert pa == pb
    assert a.client_order_id == b.client_order_id == _UUID4_CLAMPED


def test_already_legal_short_key_unchanged():
    adapter = _adapter()
    order = _order(client_order_id="abc-key", idempotency_key="abc-key")
    params = adapter._client_order_params(order)
    payload = _assert_gate_text(params, order)
    assert payload == "abc-key"
    assert params["text"] == "t-abc-key"


def test_illegal_charset_is_stripped_deterministically():
    adapter = _adapter()
    order = _order(client_order_id="mcp:abc_1", idempotency_key="mcp:abc_1")
    params = adapter._client_order_params(order)
    payload = _assert_gate_text(params, order)
    assert payload == "mcpabc_1"
    again = adapter._client_order_params(
        _order(client_order_id="mcp:abc_1", idempotency_key="mcp:abc_1")
    )
    assert again == params


def test_payload_max_keeps_prefixed_param_within_ccxt_limit():
    assert _GATE_TEXT_PAYLOAD_MAX_BYTES + len(_GATE_TEXT_PREFIX) == (
        _GATE_TEXT_PARAM_MAX_BYTES
    )
    key = "a" * 40
    clamped = GateExecutionAdapter._clamp_gate_client_order_id(key)
    assert len(clamped) == _GATE_TEXT_PAYLOAD_MAX_BYTES
    assert len(f"{_GATE_TEXT_PREFIX}{clamped}") <= _GATE_TEXT_PARAM_MAX_BYTES


def test_hard_reject_types_do_not_include_bad_request():
    adapter = _adapter()
    names = {cls.__name__ for cls in adapter._hard_reject_types()}
    assert "BadRequest" not in names


def test_recovery_order_ids_include_clamped_uuid4():
    rec = {
        "exchange_order_id": "ex-1",
        "client_order_id": _UUID4,
        "idempotency_key": _UUID4,
    }
    ids = _order_ids(rec)
    assert "ex-1" in ids
    assert _UUID4 in ids
    assert _UUID4_CLAMPED in ids
    assert ids.index(_UUID4) < ids.index(_UUID4_CLAMPED)


def test_persist_then_execute_ledger_client_order_id_matches_gate_text(monkeypatch):
    """#439 B1: ledger stores the Gate `text` payload, not the raw uuid4."""
    captured: dict = {}

    def _create(symbol, cost, params=None):
        captured["symbol"] = symbol
        captured["cost"] = cost
        captured["params"] = dict(params or {})
        return {
            "id": "ex-1",
            "status": "closed",
            "filled": 0.25,
            "average": 100.0,
            "cost": 25.0,
            "timestamp": 1_700_000_000_000,
            "fee": {"cost": 0.0005, "currency": "SOL"},
        }

    monkeypatch.setattr("execution.gate_adapter.record_live_trade", lambda rec: None)
    monkeypatch.setattr(
        "services.venue_quality.stamp_venue_for_fill",
        lambda *a, **k: {"capture": "test"},
    )
    monkeypatch.setattr("bus.writer_lease.require_lease_for_order", lambda: None)

    cfg = BotConfig(
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
    adapter = GateExecutionAdapter(cfg, PortfolioService(cfg), mode="real")
    ex = MagicMock(name="ccxt.gate")
    ex.amount_to_precision.side_effect = lambda _s, a: a
    ex.cost_to_precision.side_effect = lambda _s, a: a
    ex.load_markets.return_value = {
        SYMBOL: {"limits": {"amount": {"min": 0}, "cost": {"min": 0}}}
    }
    ex.fetch_open_orders.return_value = []
    ex.create_market_buy_order_with_cost.side_effect = _create
    adapter._exchange = ex
    adapter._fetch_usdt_balance = lambda: 10_000.0
    adapter.portfolio.execute_buy = MagicMock(
        return_value=TradeResult(
            True, "BUY", SYMBOL, amount=0.25, price=100, usdt_amount=25
        )
    )

    svc = TradingService(cfg)
    monkeypatch.setattr(svc, "refresh", lambda: svc)
    monkeypatch.setattr(svc, "can_execute", lambda *a, **k: (True, ""))
    monkeypatch.setattr(
        svc.risk,
        "evaluate",
        lambda order, *a, **k: RiskDecision(approved=True, order=order, message="ok"),
    )
    monkeypatch.setattr(
        "services.trading_service.get_execution_adapter",
        lambda *a, **k: adapter,
    )

    buy = TradeOrder("BUY", SYMBOL, 100.0, 0, usdt_amount=25.0)
    with tenant_context("default", scope="demo"):
        result = svc._execute_order_locked(
            buy,
            "4h",
            source="auto",
            idempotency_key=_UUID4,
            _lock_held=True,
        )
        stored = OrderService("demo").get_by_id(result.order_id)

    assert captured.get("params"), "create_market_buy_order_with_cost was not called"
    text = str(captured["params"].get("text") or "")
    assert text.startswith(_GATE_TEXT_PREFIX)
    payload = text[len(_GATE_TEXT_PREFIX) :]
    assert stored is not None
    assert stored["client_order_id"] == payload
    assert payload == _UUID4_CLAMPED
    assert stored["client_order_id"] != _UUID4
    assert stored["idempotency_key"] == _UUID4
    assert _clamp_gate_client_order_id(_UUID4) == payload
