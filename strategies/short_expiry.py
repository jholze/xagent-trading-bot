"""Default-off intraday hold for draft T220-05.

Missing flag stays off. A sandbox hold must be finite, positive, and strictly
under 24 hours. The untouched stack keeps the existing 4 hour base. Explicit
24 hour or 72 hour overlays are invalid and are not substituted. Session
clocks come from the caller. This module does not price funding.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from core.time_utils import ledger_datetime_utc
from strategies.short_math import is_short
from strategies.short_policy import _tier_key, shorts_allow_live, shorts_config

FLAG_KEY = "sandbox_expiry_v220"
_EXISTING_DEFAULT_HOURS = Decimal("4")
_MAX_HOURS = Decimal("4")
_CUTOFF_ZONE = "Europe/Berlin"
_PAPER_MARKETS = frozenset({"paper", "futures_usdt_testnet"})


@dataclass(frozen=True)
class HoldCap:
    valid: bool
    hours: Decimal | None
    reason: str


@dataclass(frozen=True)
class ScheduleDecision:
    open: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ExposureReport:
    state: str
    flat: bool
    reason: str


@dataclass(frozen=True)
class FundingWindow:
    accepted: bool
    reason: str
    planned_exit: datetime | None
    settlement_at: datetime | None
    funding_amount: None = None


def sandbox_expiry_enabled(config_raw: Mapping[str, Any] | None) -> bool:
    block = shorts_config(config_raw).get(FLAG_KEY)
    return isinstance(block, dict) and block.get("enabled") is True


def protection_allowed(*, position_side: str | None, reduce_only: bool) -> bool:
    """Weekend and entry denial do not turn off reduce-only protection."""
    if reduce_only is not True or not isinstance(position_side, str):
        return False
    return position_side.strip().lower() == "short"


def resolve_sandbox_hold(
    *,
    symbol: str | None = None,
    tier: str | None = None,
    lot: Mapping[str, Any] | None = None,
    config_raw: Mapping[str, Any] | None = None,
) -> HoldCap:
    """Tier, then coin, then lot. Invalid explicit caps do not fall back."""
    cfg = shorts_config(config_raw)
    lot_map = lot if isinstance(lot, dict) else {}
    tier_name = _tier_key(tier or lot_map.get("strategy_tier"))
    tier_block = cfg.get("stable") if tier_name == "stable" else cfg.get("volatile")
    chosen: Decimal | None = None
    for block in (tier_block, _coin_block(cfg, symbol, lot_map), lot_map):
        state, value = _read_cap(block)
        if state == "invalid":
            return HoldCap(False, None, "invalid")
        if state == "set":
            chosen = value
    if chosen is None:
        return HoldCap(True, _EXISTING_DEFAULT_HOURS, "default")
    return HoldCap(True, chosen, "overlay")


def schedule_allows_entry(
    *,
    now: datetime,
    opened: datetime | str,
    planned_exit: datetime | str,
    config_raw: Mapping[str, Any] | None,
    schedule: Mapping[str, Any] | None = None,
    calendar: Mapping[str, Any] | None = None,
    mode_proof: Mapping[str, Any] | None = None,
    symbol: str | None = None,
    tier: str | None = None,
    lot: Mapping[str, Any] | None = None,
    position_side: str = "flat",
) -> ScheduleDecision:
    """Clock and calendar permission only. This does not place an order."""
    if not sandbox_expiry_enabled(config_raw):
        return ScheduleDecision(False, ("sandbox_disabled",))
    reasons: list[str] = []
    proof_missing, proof_live = _proof_flags(mode_proof)
    if shorts_allow_live(config_raw) or proof_live:
        reasons.append("live_denied")
    hold = resolve_sandbox_hold(symbol=symbol, tier=tier, lot=lot, config_raw=config_raw)
    if not hold.valid or hold.hours is None:
        reasons.append("hold_invalid")
    if proof_missing:
        reasons.append("mode_proof_missing")
    if isinstance(position_side, str) and position_side.strip().lower() == "long":
        reasons.append("long_not_flipped")
    reasons.extend(_calendar_reasons(now, calendar))
    budget, buffer_reason = _explicit_budgets(schedule)
    if buffer_reason:
        reasons.append(buffer_reason)
    now_utc = ledger_datetime_utc(now)
    opened_utc = ledger_datetime_utc(opened)
    planned_utc = ledger_datetime_utc(planned_exit)
    if now_utc is None or opened_utc is None or planned_utc is None:
        reasons.append("session_unknown")
    else:
        reasons.extend(_clock_reasons(now_utc, opened_utc, planned_utc))
        session_end, boundary, session_reason = _frozen_session(schedule, opened_utc)
        if session_reason:
            reasons.append(session_reason)
        elif hold.valid and hold.hours is not None and budget is not None and session_end is not None:
            cap_end = opened_utc + _hours_td(hold.hours)
            ready = now_utc + timedelta(seconds=budget)
            outside = _holding_overdue(
                now_utc,
                cap_end=cap_end,
                session_end=session_end,
                boundary=boundary,
                session_required=True,
            ) or not _exit_inside(
                ready,
                cap_end=cap_end,
                session_end=session_end,
                boundary=boundary,
                session_required=True,
            ) or not _exit_inside(
                planned_utc,
                cap_end=cap_end,
                session_end=session_end,
                boundary=boundary,
                session_required=True,
            )
            if outside:
                reasons.append("outside_due")
    return ScheduleDecision(not reasons, tuple(_dedupe(reasons)))


def report_sandbox_exposure(
    pos: Mapping[str, Any] | None,
    *,
    now: datetime,
    config_raw: Mapping[str, Any] | None,
    mark: float,
    price_stale: bool = False,
    halted: bool | None = None,
    timed_out: bool = False,
    schedule: Mapping[str, Any] | None = None,
) -> ExposureReport:
    """Open quantity is overdue or open. Flat only when quantity is zero."""
    del mark
    amount = float((pos or {}).get("amount") or 0) if isinstance(pos, dict) else 0.0
    if not is_short(pos) or amount <= 0:
        return ExposureReport("flat", True, "qty_zero")
    if not sandbox_expiry_enabled(config_raw):
        return ExposureReport("inactive", False, "sandbox_disabled")
    if price_stale or timed_out or halted is not False:
        return ExposureReport("overdue", False, "unconfirmed")
    return _exposure_from_clock(pos, now=now, config_raw=config_raw, schedule=schedule)


def funding_acceptance(
    *,
    entry_at: datetime | str | None,
    settlement_at: datetime | str | None,
    planned_exit: datetime | str | None,
    due: datetime | str | None,
    venue_interval_hours: Any = None,
) -> FundingWindow:
    """Require entry < settlement < planned exit <= due.

    venue_interval_hours is ignored. Passing 8 does not create a settlement,
    a funding amount, or a later planned exit.
    """
    del venue_interval_hours
    planned = ledger_datetime_utc(planned_exit) if planned_exit is not None else None
    settlement = ledger_datetime_utc(settlement_at) if settlement_at is not None else None
    entry = ledger_datetime_utc(entry_at) if entry_at is not None else None
    due_at = ledger_datetime_utc(due) if due is not None else None
    if entry is None or planned is None or due_at is None or settlement is None:
        return FundingWindow(False, "funding_timestamp_unknown", planned, None, None)
    if not (entry < settlement < planned <= due_at):
        return FundingWindow(False, "funding_window", planned, settlement, None)
    return FundingWindow(True, "accepted", planned, settlement, None)


def unpriced_cover() -> dict[str, Any]:
    return {
        "source": "time_cap",
        "state": "overdue",
        "flat": False,
        "rationale": "price stale, missing, or check timed out; quantity is still open",
    }


def cover_time_decision(
    pos: Mapping[str, Any] | None,
    *,
    now: datetime,
    config_raw: Mapping[str, Any] | None,
    schedule: Mapping[str, Any] | None = None,
    halted: bool | None = None,
    symbol: str | None = None,
) -> dict[str, Any] | None:
    """Sandbox time cap. None means the lot is still inside the hold."""
    if halted is True:
        return _overdue("halted")
    hold = resolve_sandbox_hold(
        symbol=symbol or ((pos or {}).get("symbol") if isinstance(pos, dict) else None),
        tier=(pos or {}).get("strategy_tier") if isinstance(pos, dict) else None,
        lot=pos if isinstance(pos, dict) else None,
        config_raw=config_raw,
    )
    if not hold.valid or hold.hours is None:
        return _overdue("hold invalid")
    opened = ledger_datetime_utc((pos or {}).get("entry_at") or (pos or {}).get("first_buy_at"))
    now_utc = ledger_datetime_utc(now)
    if opened is None or now_utc is None:
        return _overdue("entry timestamp missing")
    if _past_frozen_hold(now_utc, opened):
        return _overdue("frozen cutoff or calendar day")
    session_required, session_end, boundary, _budget, session_reason = _session(schedule)
    if session_reason:
        return _overdue(session_reason)
    cap_end = opened + _hours_td(hold.hours)
    if _holding_overdue(
        now_utc,
        cap_end=cap_end,
        session_end=session_end,
        boundary=boundary,
        session_required=session_required,
    ):
        age_h = (now_utc - opened).total_seconds() / 3600.0
        return _overdue(f"held {age_h:.1f}h, cap {hold.hours}h")
    return None


def _exposure_from_clock(pos, *, now, config_raw, schedule) -> ExposureReport:
    hold = resolve_sandbox_hold(
        symbol=pos.get("symbol") if isinstance(pos, dict) else None,
        tier=pos.get("strategy_tier") if isinstance(pos, dict) else None,
        lot=pos if isinstance(pos, dict) else None,
        config_raw=config_raw,
    )
    if not hold.valid or hold.hours is None:
        return ExposureReport("overdue", False, "hold_invalid")
    opened = ledger_datetime_utc(pos.get("entry_at") or pos.get("first_buy_at"))
    now_utc = ledger_datetime_utc(now)
    if opened is None or now_utc is None:
        return ExposureReport("overdue", False, "opened_unknown")
    if _past_frozen_hold(now_utc, opened):
        return ExposureReport("overdue", False, "cutoff")
    session_required, session_end, boundary, _budget, session_reason = _session(schedule)
    if session_reason:
        return ExposureReport("overdue", False, session_reason)
    cap_end = opened + _hours_td(hold.hours)
    if _holding_overdue(
        now_utc,
        cap_end=cap_end,
        session_end=session_end,
        boundary=boundary,
        session_required=session_required,
    ):
        return ExposureReport("overdue", False, "due")
    return ExposureReport("open", False, "inside")


def _overdue(rationale: str) -> dict[str, Any]:
    return {
        "source": "time_cap",
        "state": "overdue",
        "flat": False,
        "rationale": rationale,
    }


def _hours_td(hours: Decimal) -> timedelta:
    micros = (hours * Decimal(3600) * Decimal(1_000_000)).to_integral_value()
    return timedelta(microseconds=int(micros))


def _coin_block(cfg, symbol, lot):
    coins = cfg.get("coins") if isinstance(cfg.get("coins"), dict) else {}
    sym = str(symbol or lot.get("symbol") or "").upper()
    if not sym:
        return None
    found = coins.get(sym) or coins.get(sym.replace("/USDT", "") + "/USDT")
    return found if isinstance(found, dict) else None


def _read_cap(block) -> tuple[str, Decimal | None]:
    if not isinstance(block, dict) or "time_cap_hours" not in block:
        return "absent", None
    parsed = _parse_hours(block.get("time_cap_hours"))
    if parsed is None:
        return "invalid", None
    return "set", parsed


def _parse_hours(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            return None
        number = Decimal(str(value))
    elif isinstance(value, str):
        try:
            number = Decimal(value.strip())
        except Exception:
            return None
    else:
        return None
    if not number.is_finite() or number <= 0 or number > _MAX_HOURS:
        return None
    return number


def _berlin(moment: datetime):
    parsed = ledger_datetime_utc(moment)
    if parsed is None:
        return None
    return parsed.astimezone(ZoneInfo(_CUTOFF_ZONE))


def _cutoff_utc(opened_utc: datetime) -> datetime | None:
    local = _berlin(opened_utc)
    if local is None:
        return None
    cutoff = local.replace(hour=20, minute=0, second=0, microsecond=0)
    return cutoff.astimezone(timezone.utc)


def _past_frozen_hold(now_utc: datetime, opened_utc: datetime) -> bool:
    now_local = _berlin(now_utc)
    opened_local = _berlin(opened_utc)
    if now_local is None or opened_local is None:
        return True
    if now_local.date() != opened_local.date() or now_local.weekday() >= 5:
        return True
    return (now_local.hour, now_local.minute, now_local.second, now_local.microsecond) >= (20, 0, 0, 0)


def _explicit_budgets(schedule):
    """Item 6: no invented minute buffer. Missing budgets deny; explicit zero is data."""
    if not isinstance(schedule, dict):
        return None, "buffer_unknown"
    total = 0
    for key in ("execution_budget_seconds", "reconcile_budget_seconds"):
        if key not in schedule:
            return None, "buffer_unknown"
        value = schedule.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None, "buffer_unknown"
        total += value
    return total, None


def _clock_reasons(now_utc: datetime, opened_utc: datetime, planned_utc: datetime) -> list[str]:
    now_local = _berlin(now_utc)
    opened_local = _berlin(opened_utc)
    planned_local = _berlin(planned_utc)
    if now_local is None or opened_local is None or planned_local is None:
        return ["session_unknown"]
    reasons: list[str] = []
    if now_local.weekday() >= 5:
        reasons.append("weekend_denied")
    elif (now_local.hour, now_local.minute, now_local.second, now_local.microsecond) >= (20, 0, 0, 0):
        reasons.append("after_cutoff")
    if planned_local.date() != opened_local.date() or now_local.date() != opened_local.date():
        reasons.append("same_calendar_day")
    return reasons


def _frozen_session(schedule, opened_utc: datetime):
    cutoff = _cutoff_utc(opened_utc)
    if cutoff is None:
        return None, None, "session_unknown"
    if not isinstance(schedule, dict) or "session_end" not in schedule:
        return cutoff, "inclusive", None
    end = ledger_datetime_utc(schedule.get("session_end"))
    if end is None:
        return None, None, "session_unknown"
    if end > cutoff:
        return None, None, "cutoff_loosened"
    boundary = schedule.get("session_boundary", "inclusive")
    if boundary not in {"inclusive", "exclusive"}:
        return None, None, "session_unknown"
    return end, boundary, None


def _session(schedule):
    if not isinstance(schedule, dict) or schedule.get("session_required") is not True:
        return False, None, None, 0, None
    boundary = schedule.get("session_boundary")
    session_end = ledger_datetime_utc(schedule.get("session_end"))
    if boundary not in {"inclusive", "exclusive"} or session_end is None:
        return True, None, None, 0, "session_unknown"
    budget = 0
    for key in ("execution_budget_seconds", "reconcile_budget_seconds"):
        if key not in schedule:
            return True, session_end, boundary, 0, "session_unknown"
        value = schedule.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return True, session_end, boundary, 0, "session_unknown"
        budget += value
    return True, session_end, boundary, budget, None


def _holding_overdue(now, *, cap_end, session_end, boundary, session_required) -> bool:
    if now >= cap_end:
        return True
    if not session_required or session_end is None:
        return False
    if boundary == "exclusive":
        return now >= session_end
    return now > session_end


def _exit_inside(ts, *, cap_end, session_end, boundary, session_required) -> bool:
    if ts > cap_end:
        return False
    if not session_required or session_end is None:
        return True
    if boundary == "exclusive":
        return ts < session_end
    return ts <= session_end


def _calendar_reasons(now, calendar) -> list[str]:
    if not isinstance(calendar, dict):
        return ["calendar_unknown"]
    tz_name = calendar.get("timezone")
    if not isinstance(tz_name, str) or not tz_name.strip():
        return ["calendar_unknown"]
    try:
        zone = ZoneInfo(tz_name.strip())
    except Exception:
        return ["calendar_unknown"]
    if "as_of" not in calendar or "max_age_seconds" not in calendar:
        return ["calendar_unknown"]
    as_of = ledger_datetime_utc(calendar.get("as_of"))
    max_age = calendar.get("max_age_seconds")
    now_utc = ledger_datetime_utc(now)
    if (
        as_of is None
        or now_utc is None
        or isinstance(max_age, bool)
        or not isinstance(max_age, int)
        or max_age < 0
        or as_of > now_utc
    ):
        return ["calendar_unknown"]
    reasons: list[str] = []
    if (now_utc - as_of).total_seconds() > max_age:
        reasons.append("calendar_stale")
    windows = calendar.get("maintenance") if "maintenance" in calendar else None
    if not isinstance(windows, list):
        reasons.append("calendar_unknown")
    else:
        for window in windows:
            parsed = _window(window)
            if parsed is None:
                reasons.append("calendar_unknown")
                break
            boundary, start, end = parsed
            if _inside_window(now_utc, boundary, start, end):
                reasons.append("maintenance")
                break
    if now_utc.astimezone(zone).weekday() >= 5 and calendar.get("weekend_admission") is not True:
        reasons.append("weekend_denied")
    return reasons


def _window(window: Any):
    if not isinstance(window, dict):
        return None
    boundary = window.get("boundary")
    if boundary not in {"inclusive", "exclusive"}:
        return None
    start = ledger_datetime_utc(window.get("start"))
    end = ledger_datetime_utc(window.get("end"))
    if start is None or end is None or end < start:
        return None
    return boundary, start, end


def _inside_window(now, boundary, start, end) -> bool:
    if boundary == "inclusive":
        return start <= now <= end
    return start < now < end


def _proof_flags(proof) -> tuple[bool, bool]:
    if not isinstance(proof, dict):
        return True, False
    missing = False
    live = False
    tenant = proof.get("tenant_id")
    if not isinstance(tenant, str) or not tenant.strip() or tenant.strip().lower() == "unknown":
        missing = True
    market = proof.get("market")
    if not isinstance(market, str) or not market.strip():
        missing = True
    elif market.strip() not in _PAPER_MARKETS:
        live = True
    if proof.get("live") is True:
        live = True
    elif proof.get("live") is not False:
        missing = True
    if proof.get("readback_ok") is not True:
        missing = True
    as_of = proof.get("as_of") if "as_of" in proof else None
    if as_of is None:
        missing = True
    elif isinstance(as_of, str):
        if not as_of.strip() or as_of.strip().lower() == "unknown":
            missing = True
    elif ledger_datetime_utc(as_of) is None:
        missing = True
    return missing, live


def _dedupe(reasons: list[str]) -> list[str]:
    seen: list[str] = []
    for reason in reasons:
        if reason not in seen:
            seen.append(reason)
    return seen
