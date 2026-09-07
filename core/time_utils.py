"""Display timezone for logs and Telegram (Railway defaults to UTC)."""

from __future__ import annotations

import os
from datetime import datetime
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