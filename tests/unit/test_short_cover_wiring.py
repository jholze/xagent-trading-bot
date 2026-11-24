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


# --- orchestrator -----------------------------------------------------------


class _Trading:
    def __init__(self):
        self.orders = []

    def execute_order(self, order, timeframe, source=None, **kwargs):
        self.orders.append(order)
        return {"executed": True}


def _cycle(cfg, lot, price=MARK, price_source=None):
    from services.signal_orchestrator import SignalOrchestrator

    orch = SignalOrchestrator.__new__(SignalOrchestrator)
    orch.config = type("Cfg", (), {"raw": cfg})()
    orch.trading = _Trading()
    analysis = type("Analysis", (), {"symbol": SYM, "timeframe": "1h"})()
    kwargs = {} if price_source is None else {"price_source": price_source}
    handled, _result = orch._cycle_short_cover(dict(lot), analysis, price, **kwargs)
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


def test_cycle_stale_quote_is_unpriced_cover_for_tagged_lot(clock):
    clock["now"] = INSIDE
    orders = _cycle(_cfg(), _lot(), price_source="stale")
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
        "-> time_cap (halted) executed=True"
    ]


@pytest.mark.parametrize("source", ["live", "redis", None])
def test_cycle_fresh_quote_does_not_unpriced_cover_tagged_lot(clock, source):
    """M3: only the ``stale`` label marks a quote stale; a live quote holds."""
    clock["now"] = INSIDE
    assert _cycle(_cfg(), _lot(), price_source=source) == []


@pytest.mark.parametrize("now", [INSIDE, datetime(2026, 11, 24, 21, 30, tzinfo=_BERLIN)])
@pytest.mark.parametrize("shorts_enabled", [True, False])
def test_cycle_untagged_lot_decision_identical_to_base_call(clock, monkeypatch, now, shorts_enabled):
    """Untagged: same decision as the pre-#826 call, whatever halt/stale/schedule say."""
    lines = []
    monkeypatch.setattr(
        "services.signal_orchestrator.log", lambda msg, level="INFO": lines.append(msg)
    )
    clock["now"] = now
    cfg = _cfg(shorts_enabled=shorts_enabled)
    lot = _lot(tagged=False)
    expected = evaluate_short_cover(dict(lot), MARK, symbol=SYM, config_raw=cfg)
    orders = _cycle(cfg, lot, price_source="stale")
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


def _hub(cfg, lot, monkeypatch, *, stale_age=None, price=MARK, out=None):
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
    hub._eval_row_in_context(SYM, price, snapshot, row_tenant=None, time_ms=1)
    if out is not None:
        out["hub"] = hub
    return executed


def test_hub_stale_tick_never_fills_tagged_lot(clock, monkeypatch):
    """#710 holds for tagged shorts too: no fill at a stale tick price.

    The tick price is far below entry, so an evaluated tick would move
    recent_low (M2) and, being past the cutoff, would cover.
    """
    clock["now"] = AFTER_CUTOFF
    writes = []
    monkeypatch.setattr(
        "strategies.positions.set_position_field",
        lambda *a, **k: writes.append(a),
    )
    out: dict = {}
    executed = _hub(_cfg(), _lot(), monkeypatch, stale_age=120.0, price=50.0, out=out)
    stats = out["hub"]._stats
    assert executed == []
    assert writes == []
    assert stats.get("stale_tagged_short_deferred") == 1
    assert stats.get("ws_stale_tick_skip") == 1


def test_hub_stale_tick_untagged_lot_not_counted_as_deferred(clock, monkeypatch):
    clock["now"] = INSIDE
    out: dict = {}
    assert _hub(_cfg(), _lot(tagged=False), monkeypatch, stale_age=120.0, out=out) == []
    assert "stale_tagged_short_deferred" not in out["hub"]._stats
    assert out["hub"]._stats.get("ws_stale_tick_skip") == 1


def test_hub_stale_tick_tagged_climax_lot_skips_climax_decision(clock, monkeypatch):
    """M5: a lot that is both tagged and climax never runs climax_cover_decision on a stale tick."""
    calls = []

    def _climax(*a, **k):
        calls.append(k)
        return {"source": "climax_stop", "rationale": "x"}

    monkeypatch.setattr("strategies.climax_fade.climax_cover_decision", _climax)
    clock["now"] = INSIDE
    out: dict = {}
    lot = _lot(short_recipe="climax_fade")
    assert _hub(_cfg(), lot, monkeypatch, stale_age=120.0, out=out) == []
    assert calls == []
    assert "stale_tagged_short_deferred" not in out["hub"]._stats


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
        "-> time_cap (halted) executed=True"
    ]


def test_hub_stale_tick_untagged_lot_still_skipped(clock, monkeypatch):
    clock["now"] = datetime(2026, 11, 24, 21, 30, tzinfo=_BERLIN)  # would time-cap if evaluated
    assert _hub(_cfg(), _lot(tagged=False), monkeypatch, stale_age=120.0) == []


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


@pytest.mark.parametrize(
    "keys, expected",
    [
        (("execution_budget_seconds", "reconcile_budget_seconds"), {"execution_budget_seconds": 0, "reconcile_budget_seconds": 0}),
        (("execution_budget_seconds",), None),
        (("reconcile_budget_seconds",), None),
        ((), None),
    ],
    ids=["both", "only_execution", "only_reconcile", "none"],
)
def test_schedule_resolver_needs_both_budget_keys(keys, expected):
    from strategies.short_expiry import sandbox_cover_schedule

    cfg = _cfg(budgets=False)
    for key in keys:
        cfg["shorts"]["sandbox_expiry_v220"][key] = 0
    assert sandbox_cover_schedule(cfg) == expected
    assert sandbox_cover_schedule({"shorts": {}}) is None


@pytest.mark.parametrize("missing", ["execution_budget_seconds", "reconcile_budget_seconds"])
def test_one_budget_key_falls_back_to_lot_budgets(clock, missing):
    """One key missing: no schedule, so the lot's stamped budgets decide (base behaviour)."""
    clock["now"] = INSIDE
    cfg = _cfg()
    del cfg["shorts"]["sandbox_expiry_v220"][missing]
    stamped = _lot(execution_budget_seconds=0, reconcile_budget_seconds=0)
    assert _cycle(cfg, stamped) == []
    unstamped = _cycle(cfg, _lot())
    assert [o.exit_rationale for o in unstamped] == ["buffer_unknown"]


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


# --- eval-queue cycle passes its own quote source (item 7) -----------------


@pytest.mark.parametrize("source", ["stale", "live"])
def test_eval_job_hands_batch_source_to_process_coin(monkeypatch, source):
    from unittest.mock import patch

    from bus.eval_queue import PRIORITY_ENTRY_15M, EvalJob
    from core.tenant_context import DEFAULT_TENANT
    from services.eval_queue_runtime import process_eval_job

    seen = {}

    class _Orchestrator:
        def begin_tenant_cycle(self):
            return None

        def process_coin(self, coin, price, *args, **kwargs):
            seen.update(kwargs)
            return {"action": "HOLD"}

    job = EvalJob(
        symbol=SYM,
        timeframe="1h",
        reason="stale_watchlist",
        priority=PRIORITY_ENTRY_15M,
        enqueued_at=0.0,
        tenant_id=DEFAULT_TENANT,
    )
    with patch(
        "storage.tenant_registry.get_tenant",
        return_value={"defaults": {"ledger_scope": "demo"}, "telegram": {}},
    ), patch(
        "services.eval_queue_runtime.get_prices_batch",
        lambda syms, *a, **k: ({SYM: MARK}, {SYM: source}) if k.get("return_sources") else {SYM: MARK},
    ), patch("services.eval_queue_runtime.load_effective_watchlist", return_value=[]):
        process_eval_job(_Orchestrator(), job)
    assert seen.get("price_source") == source


class _StopAfterCover(Exception):
    pass


@pytest.mark.parametrize("source, covered", [("stale", True), ("live", False)])
def test_process_coin_hands_price_source_to_cycle_cover(clock, monkeypatch, source, covered):
    """process_coin(..., price_source=...) reaches _cycle_short_cover (kills price_source=None)."""
    from services.signal_orchestrator import SignalOrchestrator

    clock["now"] = INSIDE
    lot = _lot()
    monkeypatch.setattr(
        "services.signal_orchestrator.find_open_position_for_symbol",
        lambda symbol, preferred_timeframe=None: ("1h", dict(lot)),
    )
    monkeypatch.setattr("strategies.positions.set_position_field", lambda *a, **k: None)

    class _Audit:
        def record(self, *a, **k):
            # The short-cover step has run; the rest of process_coin is reporting.
            raise _StopAfterCover

    orch = SignalOrchestrator.__new__(SignalOrchestrator)
    orch.config = type("Cfg", (), {"raw": _cfg()})()
    orch.trading = _Trading()
    orch.audit = _Audit()
    orch.analyze = lambda *a, **k: type(
        "Analysis", (), {"symbol": SYM, "timeframe": "1h", "rsi": 50.0}
    )()
    with pytest.raises(_StopAfterCover):
        orch.process_coin({"symbol": SYM, "timeframe": "1h"}, MARK, price_source=source)
    if covered:
        assert [o.exit_rationale[:11] for o in orch.trading.orders] == ["price stale"]
    else:
        assert orch.trading.orders == []


def test_stale_path_skips_ledger_lookup_when_flag_off(clock, monkeypatch):
    """NIT 5: with the sandbox flag off the stale-tick stat does no ledger read."""
    from services.exit_realtime.hub import ExitRealtimeHub

    hub = ExitRealtimeHub(_cfg(tagged_flag=False))
    reads = []
    monkeypatch.setattr("strategies.positions.get_position", lambda *a: reads.append(a))
    assert hub._stale_tick_reaches_tagged_short(SYM, {"timeframe": "1h"}) is False
    assert reads == []
