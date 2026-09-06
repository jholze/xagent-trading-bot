"""#305 slice 3: entries/exits split and emergency-sell bypass matrix."""

from __future__ import annotations

import pytest

from core.config import BotConfig
from core.models import RiskDecision, TradeOrder, TradeResult
from services.trading_service import (
    ENTRIES_PAUSED_MSG,
    EXITS_PAUSED_MSG,
    TradingService,
)


def _cfg(*, mode: str = "paper", entries: bool = True, exits: bool = True) -> BotConfig:
    bot = BotConfig()
    bot._raw = {
        # Direct raw config bypasses the conftest normalizer: keep these tests
        # Redis-independent (fail-closed lock / lease have their own tests).
        "architecture": {"ledger_lock_fail_closed": False, "single_writer_lease_enabled": False},
        "trading_mode": mode,
        "virtual_trading": True,
        "live_confirmed": True,
        "live": {"dry_run": True},
        "trading": {"entries_enabled": entries, "exits_enabled": exits},
    }
    return bot


def _svc(**kwargs) -> TradingService:
    return TradingService(_cfg(**kwargs))


def _order(otype: str, signal: str = "") -> TradeOrder:
    return TradeOrder(
        type=otype,
        symbol="BTC/USDT",
        price=1.0,
        amount=1.0,
        usdt_amount=1.0,
        signal=signal,
        source="auto",
    )


# (type, signal, emergency)
_CASES = [
    ("BUY", "", False),
    ("SHORT", "SHORT", False),
    ("SELL", "SELL", False),
    ("SELL", "SELL_20", False),
    ("COVER", "COVER", False),
    ("SELL", "SELL_STOP_FULL", True),
    ("SELL", "SELL_STOP_PARTIAL", True),
    ("SELL", "SELL_FULL", True),
    ("SELL", "TRAILING_STOP", True),
    ("SELL", "X_STOP_LOSS", True),
]


def _expect_allowed(otype: str, emergency: bool, entries: bool, exits: bool) -> bool:
    if otype in ("BUY", "SHORT"):
        return entries
    if otype in ("SELL", "COVER"):
        return emergency or exits
    return True


def _expect_code(otype: str, emergency: bool, entries: bool, exits: bool) -> str:
    if otype in ("BUY", "SHORT") and not entries:
        return "entries_paused"
    if otype in ("SELL", "COVER") and not emergency and not exits:
        return "exits_paused"
    return ""


@pytest.mark.parametrize("otype,signal,emergency", _CASES)
@pytest.mark.parametrize("entries,exits", [(True, True), (False, True), (True, False), (False, False)])
def test_bypass_matrix_can_execute(otype, signal, emergency, entries, exits):
    svc = _svc(mode="paper", entries=entries, exits=exits)
    order = _order(otype, signal)
    ok, reason = svc.can_execute(source="auto", order=order)
    allowed = _expect_allowed(otype, emergency, entries, exits)
    assert ok is allowed
    if allowed:
        assert reason == ""
    else:
        code = _expect_code(otype, emergency, entries, exits)
        assert code in ("entries_paused", "exits_paused")
        if code == "entries_paused":
            assert reason == ENTRIES_PAUSED_MSG
        else:
            assert reason == EXITS_PAUSED_MSG


@pytest.mark.parametrize("otype,signal,emergency", _CASES)
def test_mode_off_blocks_everything_including_emergency(otype, signal, emergency):
    svc = _svc(mode="off", entries=True, exits=True)
    ok, reason = svc.can_execute(source="auto", order=_order(otype, signal))
    assert ok is False
    assert "disabled" in reason.lower()
    assert "mode=off" in reason


def test_can_execute_without_order_ignores_entries_switch():
    """Existing callers (no order) still only see the mode gate."""
    svc = _svc(mode="paper", entries=False, exits=False)
    ok, reason = svc.can_execute()
    assert ok is True
    assert reason == ""


def test_botconfig_defaults_true_when_trading_block_missing():
    bot = BotConfig()
    bot._raw = {"trading_mode": "paper"}
    assert bot.entries_enabled is True
    assert bot.exits_enabled is True


def test_execute_order_entries_paused_sets_code(monkeypatch):
    svc = _svc(mode="paper", entries=False, exits=True)
    order = _order("BUY")
    monkeypatch.setattr(svc, "refresh", lambda: svc)
    monkeypatch.setattr(
        "services.trading_engine_runtime.should_queue_intent", lambda *a, **k: False
    )
    result = svc.execute_order(order, "4h", source="manual")
    assert result.executed is False
    assert result.code == "entries_paused"
    assert result.message == ENTRIES_PAUSED_MSG


def test_execute_order_exits_paused_sets_code(monkeypatch):
    svc = _svc(mode="paper", entries=True, exits=False)
    order = _order("SELL", "SELL")
    monkeypatch.setattr(svc, "refresh", lambda: svc)
    monkeypatch.setattr(
        "services.trading_engine_runtime.should_queue_intent", lambda *a, **k: False
    )
    result = svc.execute_order(order, "4h", source="manual")
    assert result.executed is False
    assert result.code == "exits_paused"
    assert result.message == EXITS_PAUSED_MSG


def test_execute_order_mode_off_does_not_set_pause_code(monkeypatch):
    svc = _svc(mode="off", entries=True, exits=True)
    order = _order("SELL", "SELL_STOP_FULL")
    monkeypatch.setattr(svc, "refresh", lambda: svc)
    monkeypatch.setattr(
        "services.trading_engine_runtime.should_queue_intent", lambda *a, **k: False
    )
    result = svc.execute_order(order, "4h", source="manual")
    assert result.executed is False
    assert "disabled" in result.message.lower()
    assert result.code == ""


def test_emergency_sell_allowed_when_exits_paused(monkeypatch):
    svc = _svc(mode="paper", entries=True, exits=False)
    order = _order("SELL", "SELL_STOP_FULL")
    filled = TradeResult(True, "SELL", "BTC/USDT", amount=1, price=1.0, usdt_amount=1.0)
    monkeypatch.setattr(svc, "refresh", lambda: svc)
    monkeypatch.setattr(
        "services.trading_engine_runtime.should_queue_intent", lambda *a, **k: False
    )
    monkeypatch.setattr(
        svc.risk, "evaluate", lambda *a, **k: RiskDecision(approved=True, order=order)
    )
    monkeypatch.setattr(svc.adapter, "execute", lambda *a, **k: filled)
    monkeypatch.setattr(svc, "_maybe_auto_short_after_sell", lambda *a, **k: None)
    monkeypatch.setattr(
        "notifications.telegram_commands.position_display.send_positions_snapshot",
        lambda *a, **k: None,
    )
    result = svc.execute_order(order, "4h", source="manual")
    assert result.executed is True
    assert result.code != "exits_paused"
