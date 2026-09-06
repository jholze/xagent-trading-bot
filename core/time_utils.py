"""Operator / display timezone helpers (Railway host TZ is UTC)."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_DEFAULT_TZ = "Europe/Berlin"


def display_timezone_name() -> str:
    try:
        from core.config import get_bot_config

        cfg_tz = (get_bot_config().observability_config.get("display_timezone") or "").strip()
        if cfg_tz:
            return cfg_tz
    except Exception:
        pass
    return (os.getenv("BOT_TIMEZONE") or os.getenv("TZ") or _DEFAULT_TZ).strip()


def display_tz() -> ZoneInfo:
    return ZoneInfo(display_timezone_name())


def now_display() -> datetime:
    return datetime.now(display_tz())


def format_display_time(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    return now_display().strftime(fmt)


def format_display_hms() -> str:
    return now_display().strftime("%H:%M:%S")


def format_display_with_zone(fmt: str = "%Y-%m-%d %H:%M") -> str:
    dt = now_display()
    abbrev = dt.strftime("%Z") or display_timezone_name()
    return f"{dt.strftime(fmt)} {abbrev}"


def operator_timezone_name() -> str:
    """IANA name for Telegram/operator clock.

    Config ``observability.operator_timezone``, then ``display_timezone``,
    then ``BOT_TIMEZONE``, then Europe/Berlin. Host ``TZ`` is not used —
    operator time must not follow the Railway UTC default.
    """
    try:
        from core.config import get_bot_config

        obs = get_bot_config().observability_config or {}
        for key in ("operator_timezone", "display_timezone"):
            cfg_tz = (obs.get(key) or "").strip()
            if cfg_tz:
                return cfg_tz
    except Exception:
        pass
    env = (os.getenv("BOT_TIMEZONE") or "").strip()
    if env:
        return env
    return _DEFAULT_TZ


def operator_tz() -> ZoneInfo:
    name = operator_timezone_name()
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo(_DEFAULT_TZ)


# Issue #320: OPERATOR_TZ is the operator zone. Call operator_tz() so a
# config reload is picked up; this name is the default IANA id.
OPERATOR_TZ = ZoneInfo(_DEFAULT_TZ)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def process_local_tz():
    """IANA zone of naive ``datetime.now()`` stamps, including DST history.

    ``datetime.now().astimezone().tzinfo`` is often a *current* offset (CEST)
    rather than ``ZoneInfo("Europe/Berlin")``, which mis-tags winter/spring
    dates. Prefer ``TZ`` / the zone key when available.
    """
    name = (os.environ.get("TZ") or "").strip()
    if name:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    tz = datetime.now().astimezone().tzinfo
    key = getattr(tz, "key", None)
    if key:
        try:
            return ZoneInfo(key)
        except Exception:
            pass
    return tz or timezone.utc


def parse_iso_datetime(value: str | datetime | None) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw)
    except Exception:
        return None


def to_operator_time(dt: datetime) -> datetime:
    """Return *dt* as an aware datetime in the operator timezone.

    Naive values are the process-local wall clock that ``datetime.now()`` /
    ``OrderService._now()`` writes (UTC on Railway, Europe/Berlin on the
    operator Mac) — the same rule ``to_utc`` and the calendar windows in
    ``services.order_service`` follow (frozen contract:
    ``test_naive_utc_fill_near_midnight_on_berlin_day``). They are tagged
    with ``process_local_tz()`` and converted; aware values are converted.
    Treating naive stamps as operator time would render Railway-written
    times two hours early (#320 review).
    """
    tz = operator_tz()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=process_local_tz())
    return dt.astimezone(tz)


def format_operator_time(dt: datetime | str | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    parsed = dt if isinstance(dt, datetime) else parse_iso_datetime(dt)
    if parsed is None:
        return ""
    return to_operator_time(parsed).strftime(fmt)


def to_utc(dt: datetime) -> datetime:
    """Return *dt* as an aware UTC datetime.

    Naive ledger timestamps are process-local wall clock — the same clock
    ``datetime.now().isoformat()`` / ``OrderService._now()`` writes (UTC on
    Railway, Europe/Berlin on the operator Mac). Do not change writers
    in #320. Aware values (Z / +00:00) are used as-is.

    On a Europe/Berlin host this equals OPERATOR_TZ, so Berlin behaviour
    is unchanged.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=process_local_tz())
    return dt.astimezone(timezone.utc)


def ledger_datetime_utc(value: str | datetime | None) -> datetime | None:
    """Parse a ledger ISO stamp to aware UTC (see ``to_utc``)."""
    parsed = parse_iso_datetime(value)
    if parsed is None:
        return None
    return to_utc(parsed)
