"""T220-05 draft: intraday hold under 24h. Synthetic clocks are not production cutoffs.

The 4h default is the existing resolver base. 20:00 Europe/Berlin is an example
session end in one test only. No figure is copied from an owner-freeze file.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from strategies.short_cover import evaluate_short_cover
from strategies.short_expiry import (
    funding_acceptance,
    protection_allowed,
    report_sandbox_exposure,
    resolve_sandbox_hold,
    sandbox_expiry_enabled,
    schedule_allows_entry,
)
from strategies.short_policy import resolve_short_params

_REPO = Path(__file__).resolve().parents[2]
_BERLIN = ZoneInfo("Europe/Berlin")
_PAPER = {
    "tenant_id": "fixture-tenant",
    "market": "paper",
    "live": False,
    "readback_ok": True,
    "as_of": "2026-11-10T15:00:00+00:00",
}


def _on(**shorts):
    block = {"sandbox_expiry_v220": {"enabled": True}, "allow_live": False}
    block.update(shorts)
    return {"shorts": block}


def _pos(*, opened, amount=1.0, entry=100.0, mark_tier="volatile"):
    stamp = opened.isoformat() if isinstance(opened, datetime) else opened
    return {
        "side": "short",
        "amount": amount,
        "average_entry": entry,
        "leverage": 2,
        "strategy_tier": mark_tier,
        "entry_at": stamp,
        "symbol": "AAA/USDT",
    }


def _calendar(now, **overrides):
    body = {
        "as_of": now.isoformat(),
        "max_age_seconds": 3600,
        "timezone": "Europe/Berlin",
        "weekend_admission": False,
        "maintenance": [],
    }
    body.update(overrides)
    return body


@contextmanager
def _host_tz(tz_name: str):
    old = os.environ.get("TZ")
    os.environ["TZ"] = tz_name
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


def test_ac1_flag_off_keeps_existing_cover():
    assert sandbox_expiry_enabled({}) is False
    assert sandbox_expiry_enabled({"shorts": {"sandbox_expiry_v220": {"enabled": "true"}}}) is False
    climax = {"shorts": {"climax_fade": {"enabled": True, "time_cap_hours": 16}}}
    assert sandbox_expiry_enabled(climax) is False
    raw = json.loads((_REPO / "config.json").read_text(encoding="utf-8"))
    assert raw["shorts"]["allow_live"] is False
    assert "sandbox_expiry_v220" not in raw["shorts"]
    opened = datetime(2026, 11, 10, 8, tzinfo=timezone.utc)
    cfg = {"shorts": {"volatile": {"time_cap_hours": 30, "stop_margin_pct": 0.5}}}
    hit = evaluate_short_cover(
        _pos(opened=opened),
        100.2,
        now=opened + timedelta(hours=10),
        config_raw=cfg,
    )
    assert hit is None
    legacy = {"shorts": {"volatile": {"time_cap_hours": 4, "stop_margin_pct": 0.5}}}
    capped = evaluate_short_cover(
        _pos(opened=opened),
        100.2,
        now=opened + timedelta(hours=5),
        config_raw=legacy,
    )
    assert capped["source"] == "time_cap"


def test_ac2_hold_cap_bounds_and_overlays():
    bare = resolve_short_params(config_raw={"shorts": {}})
    assert bare["time_cap_hours"] == 4.0
    default = resolve_sandbox_hold(config_raw=_on())
    assert default.valid is True
    assert default.hours == Decimal("4")
    climax = _on()
    climax["shorts"]["climax_fade"] = {"enabled": True, "time_cap_hours": 16}
    assert resolve_sandbox_hold(config_raw=climax).hours == Decimal("4")

    layered = _on(
        stable={"time_cap_hours": 8},
        coins={"AAA/USDT": {"time_cap_hours": 3}},
    )
    coin = resolve_sandbox_hold(symbol="AAA/USDT", tier="stable", config_raw=layered)
    assert coin.hours == Decimal("3")
    lot = resolve_sandbox_hold(
        symbol="AAA/USDT",
        tier="stable",
        lot={"symbol": "AAA/USDT", "time_cap_hours": 2},
        config_raw=layered,
    )
    assert lot.hours == Decimal("2")
    assert resolve_sandbox_hold(
        lot={"time_cap_hours": Decimal("23.999")},
        config_raw=_on(),
    ).valid is True

    for bad in (24, 72, 0, "24", Decimal("24"), float("inf"), None):
        hold = resolve_sandbox_hold(
            tier="volatile",
            lot={"time_cap_hours": bad},
            config_raw=_on(volatile={"time_cap_hours": 4}),
        )
        assert hold.valid is False
        assert hold.hours is None
    forced = evaluate_short_cover(
        _pos(opened=datetime(2026, 11, 10, 12, tzinfo=timezone.utc)),
        100.2,
        now=datetime(2026, 11, 10, 13, tzinfo=timezone.utc),
        config_raw=_on(volatile={"time_cap_hours": 24, "stop_margin_pct": 0.5}),
    )
    assert forced["state"] == "overdue"
    assert forced["flat"] is False


def test_ac3_session_due_and_budget():
    opened = datetime(2026, 11, 10, 16, 0, tzinfo=_BERLIN)
    session_end = datetime(2026, 11, 10, 20, 0, tzinfo=_BERLIN)
    now = opened
    base = {
        "session_required": True,
        "session_end": session_end.isoformat(),
        "execution_budget_seconds": 0,
        "reconcile_budget_seconds": 0,
    }
    inclusive = schedule_allows_entry(
        now=now,
        opened=opened,
        planned_exit=session_end,
        config_raw=_on(),
        schedule={**base, "session_boundary": "inclusive"},
        calendar=_calendar(now),
        mode_proof=_PAPER,
    )
    assert inclusive.open is True
    assert inclusive.reasons == ()
    exclusive = schedule_allows_entry(
        now=now,
        opened=opened,
        planned_exit=session_end,
        config_raw=_on(),
        schedule={**base, "session_boundary": "exclusive"},
        calendar=_calendar(now),
        mode_proof=_PAPER,
    )
    assert exclusive.open is False
    assert "outside_due" in exclusive.reasons
    later = schedule_allows_entry(
        now=now,
        opened=opened,
        planned_exit=session_end + timedelta(seconds=1),
        config_raw=_on(),
        schedule={**base, "session_boundary": "inclusive"},
        calendar=_calendar(now),
        mode_proof=_PAPER,
    )
    assert later.open is False

    early_end = datetime(2026, 11, 10, 18, 0, tzinfo=_BERLIN)
    tight = {
        "session_required": True,
        "session_end": early_end.isoformat(),
        "session_boundary": "exclusive",
        "execution_budget_seconds": 300,
        "reconcile_budget_seconds": 300,
    }
    fits = datetime(2026, 11, 10, 17, 49, tzinfo=_BERLIN)
    assert schedule_allows_entry(
        now=fits,
        opened=opened,
        planned_exit=fits + timedelta(minutes=10),
        config_raw=_on(),
        schedule=tight,
        calendar=_calendar(fits),
        mode_proof=_PAPER,
    ).open is True
    misses = datetime(2026, 11, 10, 17, 50, tzinfo=_BERLIN)
    missed = schedule_allows_entry(
        now=misses,
        opened=opened,
        planned_exit=fits + timedelta(minutes=10),
        config_raw=_on(),
        schedule=tight,
        calendar=_calendar(misses),
        mode_proof=_PAPER,
    )
    assert missed.open is False
    assert "outside_due" in missed.reasons

    missing_budget = dict(tight)
    del missing_budget["reconcile_budget_seconds"]
    unknown = schedule_allows_entry(
        now=fits,
        opened=opened,
        planned_exit=fits + timedelta(minutes=10),
        config_raw=_on(),
        schedule=missing_budget,
        calendar=_calendar(fits),
        mode_proof=_PAPER,
    )
    assert unknown.open is False
    assert "session_unknown" in unknown.reasons

    later_session = {
        "session_required": True,
        "session_end": datetime(2026, 11, 10, 22, 0, tzinfo=_BERLIN).isoformat(),
        "session_boundary": "inclusive",
        "execution_budget_seconds": 0,
        "reconcile_budget_seconds": 0,
    }
    cap_binds = schedule_allows_entry(
        now=opened,
        opened=opened,
        planned_exit=datetime(2026, 11, 10, 20, 0, tzinfo=_BERLIN),
        config_raw=_on(),
        schedule=later_session,
        calendar=_calendar(opened),
        mode_proof=_PAPER,
    )
    assert cap_binds.open is True
    past_cap = schedule_allows_entry(
        now=opened,
        opened=opened,
        planned_exit=datetime(2026, 11, 10, 20, 0, 1, tzinfo=_BERLIN),
        config_raw=_on(),
        schedule=later_session,
        calendar=_calendar(opened),
        mode_proof=_PAPER,
    )
    assert past_cap.open is False


def test_ac4_timestamps_restart_and_unpriced():
    now_cest = datetime(2026, 9, 27, 16, 1, 13, tzinfo=timezone.utc)
    cfg = _on(volatile={"time_cap_hours": 4, "stop_margin_pct": 0.5})
    naive_5h = datetime(2026, 9, 27, 13, 1, 13)
    with _host_tz("Europe/Berlin"):
        hit = evaluate_short_cover(
            _pos(opened=naive_5h),
            100.2,
            now=now_cest,
            config_raw=cfg,
        )
    assert hit["source"] == "time_cap"
    assert hit["flat"] is False
    assert hit["state"] == "overdue"
    naive_3h = datetime(2026, 9, 27, 15, 1, 13)
    with _host_tz("Europe/Berlin"):
        quiet = evaluate_short_cover(
            _pos(opened=naive_3h),
            100.2,
            now=now_cest,
            config_raw=cfg,
        )
    assert quiet is None

    opened = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
    pos = _pos(opened=opened)
    due_now = opened + timedelta(hours=5)
    first = report_sandbox_exposure(pos, now=due_now, config_raw=cfg, mark=100.2, halted=False)
    second = report_sandbox_exposure(pos, now=due_now, config_raw=cfg, mark=100.2, halted=False)
    assert first.state == second.state == "overdue"
    assert first.flat is False and second.flat is False
    assert pos["amount"] == 1.0
    flat = report_sandbox_exposure(
        _pos(opened=opened, amount=0),
        now=due_now,
        config_raw=cfg,
        mark=100.2,
        halted=False,
    )
    assert flat.state == "flat" and flat.flat is True

    fresh = opened + timedelta(hours=1)
    for kwargs in (
        {"price_stale": True, "halted": False},
        {"halted": True},
        {"timed_out": True, "halted": False},
    ):
        report = report_sandbox_exposure(
            pos, now=fresh, config_raw=cfg, mark=100.2, **kwargs
        )
        assert report.state == "overdue" and report.flat is False
    unknown_halt = report_sandbox_exposure(
        pos, now=fresh, config_raw=cfg, mark=100.2, halted=None
    )
    assert unknown_halt.state == "overdue" and unknown_halt.flat is False
    healthy = report_sandbox_exposure(
        pos, now=fresh, config_raw=cfg, mark=100.2, halted=False
    )
    assert healthy.state == "open" and healthy.flat is False
    stale_cover = evaluate_short_cover(
        pos, 130.0, now=fresh, config_raw=cfg, price_stale=True
    )
    assert stale_cover["state"] == "overdue"
    assert stale_cover["source"] != "trailing_stop"


def test_ac5_weekend_calendar_and_funding():
    saturday = datetime(2026, 11, 14, 12, 0, tzinfo=_BERLIN)
    opened = saturday - timedelta(hours=1)
    weekend = schedule_allows_entry(
        now=saturday,
        opened=opened,
        planned_exit=opened + timedelta(hours=2),
        config_raw=_on(),
        schedule={"session_required": False},
        calendar=_calendar(saturday),
        mode_proof=_PAPER,
    )
    assert weekend.open is False
    assert "weekend_denied" in weekend.reasons
    opened_ok = schedule_allows_entry(
        now=saturday,
        opened=opened,
        planned_exit=opened + timedelta(hours=2),
        config_raw=_on(),
        schedule={"session_required": False},
        calendar=_calendar(saturday, weekend_admission=True),
        mode_proof=_PAPER,
    )
    assert opened_ok.open is True
    assert protection_allowed(position_side="short", reduce_only=True) is True
    stop_cfg = _on(volatile={"time_cap_hours": 4, "stop_margin_pct": 0.10})
    stopped = evaluate_short_cover(
        _pos(opened=opened), 105.0, now=saturday, config_raw=stop_cfg
    )
    assert stopped["source"] == "trailing_stop"

    tuesday = datetime(2026, 11, 10, 12, 0, tzinfo=_BERLIN)
    assert schedule_allows_entry(
        now=tuesday,
        opened=tuesday,
        planned_exit=tuesday + timedelta(hours=2),
        config_raw=_on(),
        schedule={"session_required": False},
        calendar=_calendar(tuesday),
        mode_proof=_PAPER,
    ).open is True

    stale_at = tuesday - timedelta(seconds=3601)
    stale = _calendar(stale_at, max_age_seconds=3600)
    aged = schedule_allows_entry(
        now=tuesday,
        opened=tuesday,
        planned_exit=tuesday + timedelta(hours=2),
        config_raw=_on(),
        schedule={"session_required": False},
        calendar=stale,
        mode_proof=_PAPER,
    )
    assert aged.open is False and "calendar_stale" in aged.reasons
    exact = _calendar(tuesday - timedelta(seconds=3600), max_age_seconds=3600)
    assert schedule_allows_entry(
        now=tuesday,
        opened=tuesday,
        planned_exit=tuesday + timedelta(hours=2),
        config_raw=_on(),
        schedule={"session_required": False},
        calendar=exact,
        mode_proof=_PAPER,
    ).open is True
    missing_windows = _calendar(tuesday)
    del missing_windows["maintenance"]
    assert "calendar_unknown" in schedule_allows_entry(
        now=tuesday,
        opened=tuesday,
        planned_exit=tuesday + timedelta(hours=2),
        config_raw=_on(),
        schedule={"session_required": False},
        calendar=missing_windows,
        mode_proof=_PAPER,
    ).reasons

    start = datetime(2026, 11, 10, 12, 0, tzinfo=_BERLIN)
    end = datetime(2026, 11, 10, 13, 0, tzinfo=_BERLIN)
    window = {"start": start.isoformat(), "end": end.isoformat(), "boundary": "inclusive"}
    assert schedule_allows_entry(
        now=start,
        opened=start,
        planned_exit=start + timedelta(hours=1),
        config_raw=_on(),
        schedule={"session_required": False},
        calendar=_calendar(start, maintenance=[window]),
        mode_proof=_PAPER,
    ).open is False
    assert schedule_allows_entry(
        now=end,
        opened=start,
        planned_exit=end,
        config_raw=_on(),
        schedule={"session_required": False},
        calendar=_calendar(end, maintenance=[window]),
        mode_proof=_PAPER,
    ).open is False
    after = end + timedelta(seconds=1)
    assert schedule_allows_entry(
        now=after,
        opened=start,
        planned_exit=after + timedelta(minutes=30),
        config_raw=_on(),
        schedule={"session_required": False},
        calendar=_calendar(after, maintenance=[window]),
        mode_proof=_PAPER,
    ).open is True
    exclusive_window = dict(window, boundary="exclusive")
    assert schedule_allows_entry(
        now=start,
        opened=start,
        planned_exit=start + timedelta(hours=1),
        config_raw=_on(),
        schedule={"session_required": False},
        calendar=_calendar(start, maintenance=[exclusive_window]),
        mode_proof=_PAPER,
    ).open is True
    inside = start + timedelta(seconds=1)
    assert schedule_allows_entry(
        now=inside,
        opened=start,
        planned_exit=inside + timedelta(minutes=30),
        config_raw=_on(),
        schedule={"session_required": False},
        calendar=_calendar(inside, maintenance=[exclusive_window]),
        mode_proof=_PAPER,
    ).open is False
    assert schedule_allows_entry(
        now=end,
        opened=start,
        planned_exit=end,
        config_raw=_on(),
        schedule={"session_required": False},
        calendar=_calendar(end, maintenance=[exclusive_window]),
        mode_proof=_PAPER,
    ).open is True

    entry = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
    settlement = entry + timedelta(hours=1)
    planned = entry + timedelta(hours=2)
    due = entry + timedelta(hours=3)
    ok = funding_acceptance(
        entry_at=entry,
        settlement_at=settlement,
        planned_exit=planned,
        due=due,
        venue_interval_hours=None,
    )
    assert ok.accepted is True
    assert ok.funding_amount is None
    assert funding_acceptance(
        entry_at=entry, settlement_at=entry, planned_exit=planned, due=due
    ).accepted is False
    assert funding_acceptance(
        entry_at=entry, settlement_at=planned, planned_exit=planned, due=due
    ).accepted is False
    assert funding_acceptance(
        entry_at=entry, settlement_at=settlement, planned_exit=due, due=due
    ).accepted is True
    moved = funding_acceptance(
        entry_at=entry,
        settlement_at=settlement,
        planned_exit=due + timedelta(seconds=1),
        due=due,
        venue_interval_hours=8,
    )
    assert moved.accepted is False
    assert moved.planned_exit == due + timedelta(seconds=1)
    assert moved.funding_amount is None
    missing = funding_acceptance(
        entry_at=entry,
        settlement_at=None,
        planned_exit=planned,
        due=due,
        venue_interval_hours=8,
    )
    assert missing.accepted is False
    assert missing.settlement_at is None
    assert missing.funding_amount is None
    source = Path(
        __import__("strategies.short_expiry", fromlist=["x"]).__file__
    ).read_text(encoding="utf-8")
    assert "funding_cost_usdt" not in source


def test_missing_mode_proof_denies_entry():
    now = datetime(2026, 11, 10, 12, tzinfo=_BERLIN)
    decision = schedule_allows_entry(
        now=now,
        opened=now,
        planned_exit=now + timedelta(hours=2),
        config_raw=_on(),
        schedule={"session_required": False},
        calendar=_calendar(now),
        mode_proof=None,
    )
    assert decision.open is False
    assert "mode_proof_missing" in decision.reasons
