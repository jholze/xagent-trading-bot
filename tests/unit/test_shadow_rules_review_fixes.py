"""PR #747 review fixes: oracle-state source marker, cached Gate client, hook after lease."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from core.models import RiskDecision, TradeOrder, trade_ctx_fields
from services import shadow_rules as sr


@pytest.fixture(autouse=True)
def _isolated():
    sr.reset_for_tests()
    yield
    sr.reset_for_tests()


def test_decision_time_state_is_marked_decision(monkeypatch):
    monkeypatch.setattr("strategies.ctx_axes.read_oracle_state", lambda: "RISK_OFF")
    tagged = TradeOrder("BUY", "DEC/USDT", 1.0, 0, usdt_amount=100, ctx_oracle_state="RISK_ON")
    fields = trade_ctx_fields(tagged)
    assert fields["ctx_oracle_state"] == "RISK_ON"
    assert fields["ctx_oracle_state_src"] == "decision"


def test_record_time_state_is_marked_record(monkeypatch):
    monkeypatch.setattr("strategies.ctx_axes.read_oracle_state", lambda: "RISK_OFF")
    dca = TradeOrder("BUY", "DCA/USDT", 1.0, 0, usdt_amount=100, source="dca")
    fields = trade_ctx_fields(dca)
    assert fields["ctx_oracle_state"] == "RISK_OFF"
    assert fields["ctx_oracle_state_src"] == "record"
    assert trade_ctx_fields(None)["ctx_oracle_state_src"] == "record"


def test_no_state_has_no_marker(monkeypatch):
    monkeypatch.setattr("strategies.ctx_axes.read_oracle_state", lambda: None)
    fields = trade_ctx_fields(TradeOrder("SELL", "X/USDT", 1.0, 1.0))
    assert fields["ctx_oracle_state"] is None
    assert "ctx_oracle_state_src" not in fields


def test_trade_history_ctx_keeps_marker():
    from services.portfolio_service import _ctx_ledger_fields

    assert _ctx_ledger_fields({"ctx_oracle_state": "CRASH", "ctx_oracle_state_src": "record"})[
        "ctx_oracle_state_src"
    ] == "record"
    assert "ctx_oracle_state_src" not in _ctx_ledger_fields({"ctx_oracle_state": None})


def test_order_documents_carry_the_marker():
    from services.market_oracle.store import store_snapshot
    from services.order_service import OrderService

    store_snapshot({"state": "CRASH", "source": "market_oracle"})
    svc = OrderService()
    stop = TradeOrder("SELL", "MRK/USDT", 1.0, 5.0, source="exit_ws", signal="SELL_STOP_FULL")
    rec = svc.create_from_request(stop, telegram_token="mrk-1")
    assert (rec["ctx_oracle_state"], rec["ctx_oracle_state_src"]) == ("CRASH", "record")
    rej = svc.record_rejected(
        TradeOrder("BUY", "MRK/USDT", 1.0, 0, usdt_amount=50, source="auto", ctx_oracle_state="RISK_ON"),
        RiskDecision(approved=False, message="no", code="x"),
    )
    assert (rej["ctx_oracle_state"], rej["ctx_oracle_state_src"]) == ("RISK_ON", "decision")


def test_marker_constants():
    from core import models

    assert (models.ORACLE_STATE_SRC_DECISION, models.ORACLE_STATE_SRC_RECORD) == ("decision", "record")


def test_public_gate_client_is_created_once(monkeypatch):
    import ccxt

    made: list = []

    class FakeGate:
        def __init__(self, cfg):
            made.append(cfg)

    monkeypatch.setattr(sr, "_exchange", None, raising=False)
    monkeypatch.setattr(ccxt, "gate", FakeGate)
    first = sr._public_gate()
    second = sr._public_gate()
    assert first is second
    assert len(made) == 1
    assert made[0]["enableRateLimit"] is True


def test_entry_hook_runs_after_the_writer_lease_check():
    from services.trading_service import TradingService
    from storage.errors import LedgerUnavailable

    order = TradeOrder(type="BUY", symbol="LEASE/USDT", price=2.0, qty=0, usdt_amount=100, signal="BUY")
    with patch("bus.writer_lease.require_lease_for_order", side_effect=LedgerUnavailable("standby")), patch(
        "services.shadow_rules.observe_entry_candidate"
    ) as hook:
        result = TradingService().execute_order(order, "4h", source="auto")
    assert result.executed is False
    hook.assert_not_called()


def test_entry_hook_still_runs_when_the_lease_is_held():
    from services.trading_service import TradingService

    order = TradeOrder(type="BUY", symbol="LEASE/USDT", price=2.0, qty=0, usdt_amount=100, signal="BUY")
    sentinel = RuntimeError("stop after hook")
    with patch("bus.writer_lease.require_lease_for_order", return_value=None), patch(
        "services.shadow_rules.observe_entry_candidate"
    ) as hook, patch(
        "services.trading_engine_runtime.should_queue_intent", side_effect=sentinel
    ):
        with pytest.raises(RuntimeError, match="stop after hook"):
            TradingService().execute_order(order, "4h", source="auto")
    hook.assert_called_once()
