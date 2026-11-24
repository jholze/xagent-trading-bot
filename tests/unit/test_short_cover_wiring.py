"""#826: production short-cover callers pass halt, stale, timeout and schedule.

Drives the real callers, ``SignalOrchestrator._cycle_short_cover`` and
``ExitRealtimeHub._eval_row_in_context``, with fakes. New #826 modules are
imported inside the tests so this file still collects on the base commit.
Synthetic symbols only, no network, no ledger writes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from strategies.short_cover import evaluate_short_cover

_BERLIN = ZoneInfo("Europe/Berlin")
SYM = "AAA/USDT"
# Tuesday. 17:00 entry, 18:00 inside the 4h hold, 20:00:01 past the cutoff.
OPENED = datetime(2026, 11, 24, 17, 0, tzinfo=_BERLIN)
INSIDE = datetime(2026, 11, 24, 18, 0, tzinfo=_BERLIN)
AFTER_CUTOFF = datetime(2026, 11, 24, 20, 0, 1, tzinfo=_BERLIN)
MARK = 100.2  # no stop, no trail, no RSI cover


def _cfg(*, shorts_enabled=True, budgets=True, tagged_flag=True):
    flag = {"enabled": tagged_flag}
    if budgets:
        flag["execution_budget_seconds"] = 0
        flag["reconcile_budget_seconds"] = 0
    return {
        "shorts": {
            "enabled": shorts_enabled,
            "allow_live": False,
            "sandbox_expiry_v220": flag,
            "testnet_futures": {"paper_start": {}},
        },
        "exit_realtime": {
            "enabled": True,
            "mode": "live",
            "sources": [],
            "live_cooldown_sec": 0.01,
            "max_tick_age_sec": 30,
        },
    }


def _lot(*, tagged=True, opened=OPENED, **extra):
    lot = {
        "side": "short",
        "amount": 1.0,
        "average_entry": 100.0,
        "leverage": 2,
        "strategy_tier": "volatile",
        "entry_at": opened.isoformat(),
        "symbol": SYM,
        "timeframe": "1h",
        "recent_low": MARK,
    }
    if tagged:
        lot["sandbox_path"] = "v220_paper"
    lot.update(extra)
    return lot


@pytest.fixture
def clock(monkeypatch):
    """Pin evaluate_short_cover's ``datetime.now``."""

    state = {"now": INSIDE}

    class _Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            moment = state["now"].astimezone(timezone.utc)
            return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)

    monkeypatch.setattr("strategies.short_cover.datetime", _Fixed)
    return state


@pytest.fixture(autouse=True)
def _no_stale_quote(monkeypatch):
    import price_fetcher

    monkeypatch.setattr(price_fetcher, "_last_quote_source", {}, raising=False)


# --- orchestrator -----------------------------------------------------------


class _Trading:
    def __init__(self):
        self.orders = []

    def execute_order(self, order, timeframe, source=None, **kwargs):
        self.orders.append(order)
        return {"executed": True}


def _cycle(cfg, lot, price=MARK, monkeypatch=None):
    from services.signal_orchestrator import SignalOrchestrator

    orch = SignalOrchestrator.__new__(SignalOrchestrator)
    orch.config = type("Cfg", (), {"raw": cfg})()
    orch.trading = _Trading()
    analysis = type("Analysis", (), {"symbol": SYM, "timeframe": "1h"})()
    handled, _result = orch._cycle_short_cover(dict(lot), analysis, price)
    assert handled is True
    return orch.trading.orders


def test_cycle_schedule_reaches_cover_tagged_lot_holds_inside_window(clock):
    """Budgets come from the flag block. Base passed none -> buffer_unknown cover."""
    clock["now"] = INSIDE
    orders = _cycle(_cfg(), _lot())
    assert orders == []


def test_cycle_after_berlin_cutoff_covers_tagged_lot(clock):
    clock["now"] = AFTER_CUTOFF
    orders = _cycle(_cfg(), _lot())
    assert len(orders) == 1
    assert orders[0].type == "COVER"
    assert orders[0].exit_source == "time_cap"
    assert orders[0].exit_rationale == "frozen cutoff or calendar day"


def test_cycle_halt_reaches_cover_tagged_lot(clock):
    clock["now"] = INSIDE
    orders = _cycle(_cfg(shorts_enabled=False), _lot())
    assert len(orders) == 1
    assert orders[0].exit_source == "time_cap"
    assert orders[0].exit_rationale == "halted"


def test_cycle_stale_quote_is_unpriced_cover_for_tagged_lot(clock, monkeypatch):
    import price_fetcher

    clock["now"] = INSIDE
    monkeypatch.setattr(price_fetcher, "_last_quote_source", {SYM: "stale"}, raising=False)
    orders = _cycle(_cfg(), _lot())
    assert len(orders) == 1
    assert orders[0].exit_source == "time_cap"
    assert orders[0].exit_rationale.startswith("price stale")


def test_cycle_logs_one_wiring_line_for_tagged_lot(clock, monkeypatch):
    lines = []
    monkeypatch.setattr(
        "services.signal_orchestrator.log", lambda msg, level="INFO": lines.append(msg)
    )
    clock["now"] = INSIDE
    _cycle(_cfg(shorts_enabled=False), _lot())
    wiring = [line for line in lines if line.startswith("short_cover tagged")]
    assert wiring == [
        f"short_cover tagged {SYM} halt=True stale=False timeout=False sched=set "
        "-> time_cap (halted)"
    ]


@pytest.mark.parametrize("now", [INSIDE, datetime(2026, 11, 24, 21, 30, tzinfo=_BERLIN)])
@pytest.mark.parametrize("shorts_enabled", [True, False])
def test_cycle_untagged_lot_decision_identical_to_base_call(clock, monkeypatch, now, shorts_enabled):
    """Untagged: same decision as the pre-#826 call, whatever halt/stale/schedule say."""
    import price_fetcher

    monkeypatch.setattr(price_fetcher, "_last_quote_source", {SYM: "stale"}, raising=False)
    lines = []
    monkeypatch.setattr(
        "services.signal_orchestrator.log", lambda msg, level="INFO": lines.append(msg)
    )
    clock["now"] = now
    cfg = _cfg(shorts_enabled=shorts_enabled)
    lot = _lot(tagged=False)
    expected = evaluate_short_cover(dict(lot), MARK, symbol=SYM, config_raw=cfg)
    orders = _cycle(cfg, lot)
    if expected is None:
        assert orders == []
    else:
        assert [(o.exit_source, o.exit_rationale) for o in orders] == [
            (expected["source"], expected["rationale"][:240])
        ]
    assert not [line for line in lines if line.startswith("short_cover tagged")]


def test_cycle_untagged_time_cap_still_fires(clock):
    clock["now"] = datetime(2026, 11, 24, 21, 30, tzinfo=_BERLIN)  # 4.5h > 4h volatile cap
    orders = _cycle(_cfg(), _lot(tagged=False))
    assert [o.exit_source for o in orders] == ["time_cap"]
    assert orders[0].exit_rationale.startswith("held 4.5h")


def test_cycle_input_failure_keeps_untagged_path(clock, monkeypatch):
    """A broken resolver never suppresses an untagged cover."""

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("strategies.short_policy.shorts_enabled", _boom)
    monkeypatch.setattr("strategies.short_expiry.sandbox_cover_schedule", _boom, raising=False)
    clock["now"] = datetime(2026, 11, 24, 21, 30, tzinfo=_BERLIN)
    orders = _cycle(_cfg(), _lot(tagged=False))
    assert [o.exit_source for o in orders] == ["time_cap"]


# --- exit_realtime hub ------------------------------------------------------


def _hub(cfg, lot, monkeypatch, *, stale_age=None):
    from services.exit_realtime.hub import ExitRealtimeHub

    hub = ExitRealtimeHub(cfg)
    executed = []

    def _exec(**kwargs):
        executed.append(kwargs)
        return {"ok": True, "executed": True, "message": "ok"}

    monkeypatch.setattr("services.exit_realtime.hub.try_execute_trail_exit", _exec)
    monkeypatch.setattr("strategies.positions.get_position", lambda s, tf: dict(lot))
    monkeypatch.setattr(
        "strategies.position_lock.attach_lock_from_ledger", lambda p, s, tf: p
    )
    monkeypatch.setattr(
        "strategies.position_lock.auto_sell_blocked", lambda p, src: (False, "")
    )
    monkeypatch.setattr(hub, "stale_tick_age", lambda time_ms, now=None: stale_age)
    monkeypatch.setattr(hub, "_drop_book_row", lambda *a, **k: None)
    monkeypatch.setattr(hub, "_broadcast_gui", lambda *a, **k: None)
    snapshot = {"symbol": SYM, "timeframe": "1h", "position": dict(lot), "strategy_params": {}}
    hub._eval_row_in_context(SYM, MARK, snapshot, row_tenant=None, time_ms=1)
    return executed


def test_hub_stale_tick_is_unpriced_cover_for_tagged_lot(clock, monkeypatch):
    """Base skipped every stale tick (#710). Tagged lots now get #809's unpriced cover."""
    clock["now"] = INSIDE
    executed = _hub(_cfg(), _lot(), monkeypatch, stale_age=120.0)
    assert len(executed) == 1
    assert executed[0]["action"] == "COVER"
    assert executed[0]["exit_source"] == "time_cap"
    assert executed[0]["rationale"].startswith("price stale")


def test_hub_schedule_reaches_cover_tagged_lot_holds(clock, monkeypatch):
    clock["now"] = INSIDE
    assert _hub(_cfg(), _lot(), monkeypatch, stale_age=1.0) == []


def test_hub_after_berlin_cutoff_covers_tagged_lot(clock, monkeypatch):
    clock["now"] = AFTER_CUTOFF
    executed = _hub(_cfg(), _lot(), monkeypatch, stale_age=1.0)
    assert [e["rationale"] for e in executed] == ["frozen cutoff or calendar day"]


def test_hub_halt_reaches_cover_tagged_lot(clock, monkeypatch):
    clock["now"] = INSIDE
    lines = []
    monkeypatch.setattr("services.exit_realtime.hub.log", lambda msg, level="INFO": lines.append(msg))
    executed = _hub(_cfg(shorts_enabled=False), _lot(), monkeypatch, stale_age=1.0)
    assert [e["rationale"] for e in executed] == ["halted"]
    assert [line for line in lines if line.startswith("short_cover tagged")] == [
        f"short_cover tagged {SYM} halt=True stale=False timeout=False sched=set "
        "-> time_cap (halted)"
    ]


def test_hub_stale_tick_untagged_lot_still_skipped(clock, monkeypatch):
    clock["now"] = datetime(2026, 11, 24, 21, 30, tzinfo=_BERLIN)  # would time-cap if evaluated
    assert _hub(_cfg(), _lot(tagged=False), monkeypatch, stale_age=120.0) == []


def test_hub_stale_tick_climax_lot_still_skipped(clock, monkeypatch):
    clock["now"] = INSIDE
    lot = _lot(short_recipe="climax_fade")
    assert _hub(_cfg(), lot, monkeypatch, stale_age=120.0) == []


@pytest.mark.parametrize("now", [INSIDE, datetime(2026, 11, 24, 21, 30, tzinfo=_BERLIN)])
def test_hub_untagged_lot_decision_identical_to_base_call(clock, monkeypatch, now):
    clock["now"] = now
    cfg = _cfg(shorts_enabled=False)
    lot = _lot(tagged=False)
    expected = evaluate_short_cover(dict(lot), MARK, symbol=SYM, config_raw=cfg)
    executed = _hub(cfg, lot, monkeypatch, stale_age=1.0)
    if expected is None:
        assert executed == []
    else:
        assert [(e["exit_source"], e["rationale"]) for e in executed] == [
            (expected["source"], expected["rationale"])
        ]


# --- resolver units ---------------------------------------------------------


def test_schedule_resolver_reads_only_budgets_from_flag_block():
    from strategies.short_expiry import sandbox_cover_schedule

    assert sandbox_cover_schedule(_cfg()) == {
        "execution_budget_seconds": 0,
        "reconcile_budget_seconds": 0,
    }
    assert sandbox_cover_schedule(_cfg(budgets=False)) is None
    assert sandbox_cover_schedule({"shorts": {}}) is None


def test_inputs_empty_for_untagged_and_flag_off():
    from strategies.short_cover_wiring import sandbox_cover_inputs

    assert sandbox_cover_inputs(_lot(tagged=False), _cfg(), price_stale=True) == {}
    assert sandbox_cover_inputs(_lot(), _cfg(tagged_flag=False), price_stale=True) == {}
    got = sandbox_cover_inputs(_lot(), _cfg(shorts_enabled=False), price_stale=lambda: True)
    assert got == {
        "price_stale": True,
        "timed_out": False,
        "halted": True,
        "sandbox_schedule": {"execution_budget_seconds": 0, "reconcile_budget_seconds": 0},
    }
