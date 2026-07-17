"""Load memory.macro / sessions / polymarket / calendar_risk config."""

from __future__ import annotations

import os
from typing import Any


def _raw(config: dict | None = None) -> dict:
    if config is not None:
        return config
    try:
        from core.config import get_bot_config

        return get_bot_config().raw or {}
    except Exception:
        return {}


def macro_config(config: dict | None = None) -> dict[str, Any]:
    mem = (_raw(config).get("memory") or {})
    return dict(mem.get("macro") or {})


def sessions_config(config: dict | None = None) -> dict[str, Any]:
    mem = (_raw(config).get("memory") or {})
    return dict(mem.get("sessions") or {})


def polymarket_config(config: dict | None = None) -> dict[str, Any]:
    mem = (_raw(config).get("memory") or {})
    return dict(mem.get("polymarket") or {})


def calendar_risk_config(config: dict | None = None) -> dict[str, Any]:
    mem = (_raw(config).get("memory") or {})
    return dict(mem.get("calendar_risk") or {})


def macro_enabled(config: dict | None = None) -> bool:
    if os.environ.get("MEMORY_MACRO", "").strip().lower() in ("0", "false", "no", "off"):
        return False
    m = macro_config(config)
    s = sessions_config(config)
    p = polymarket_config(config)
    # default on if any subsection present or empty (we enable via config.json)
    if "enabled" in m:
        return bool(m.get("enabled"))
    return bool(m or s or p) or True
