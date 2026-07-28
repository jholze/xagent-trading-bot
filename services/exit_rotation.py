"""Runtime exit profile overlay (rot_mid etc.) — no permanent config rewrite.

Rollback: exit_rotation.enabled=false OR profile=base.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

_DEFAULTS = {
    "enabled": False,
    "profile": "base",  # base | rot_mid | rot_agg
}

# Only knobs we override; rest stays from tier strategy_params
PROFILES: dict[str, dict[str, dict]] = {
    "base": {},
    "rot_mid": {
        "trailing_take_profit": {
            "arm_gain_pct": 10,
            "min_gain_pct": 6,
            "min_gain_pct_floor": 6,
        },
        "profit_max_lifetime": {
            "max_hours": 48,
        },
    },
    "rot_agg": {
        "trailing_take_profit": {
            "arm_gain_pct": 8,
            "min_gain_pct": 5,
            "min_gain_pct_floor": 5,
            "trail_pct": 5,
            "cooldown_hours": 4,
        },
        "profit_max_lifetime": {
            "max_hours": 24,
        },
    },
}


def exit_rotation_config(config: dict | None = None) -> dict:
    raw = {}
    if isinstance(config, dict):
        sec = config.get("exit_rotation")
        if isinstance(sec, dict):
            raw = sec
    out = {**_DEFAULTS, **raw}
    out["enabled"] = bool(out.get("enabled", False))
    prof = str(out.get("profile") or "base").strip().lower()
    if prof not in PROFILES:
        prof = "base"
    out["profile"] = prof
    return out


def _root_config() -> dict | None:
    try:
        from data_manager import get_config

        c = get_config()
        return c if isinstance(c, dict) else None
    except Exception:
        try:
            from data_manager import load_config

            return load_config()
        except Exception:
            return None


def apply_exit_section_overlay(
    section_cfg: dict,
    section: str,
    *,
    root_config: dict | None = None,
) -> dict:
    """Merge profile knobs into a TTP or PML section dict."""
    root = root_config if root_config is not None else _root_config()
    er = exit_rotation_config(root)
    if not er.get("enabled"):
        return dict(section_cfg or {})
    pack = PROFILES.get(er["profile"]) or {}
    overlay = pack.get(section) or {}
    if not overlay:
        return dict(section_cfg or {})
    out = dict(section_cfg or {})
    out.update(overlay)
    return out


def apply_exit_rotation_to_strategy_params(
    strategy_params: dict | None,
    *,
    root_config: dict | None = None,
) -> dict:
    """Deep-merge exit profile into full strategy_params (optional bulk path)."""
    sp = deepcopy(strategy_params or {})
    root = root_config if root_config is not None else _root_config()
    er = exit_rotation_config(root)
    if not er.get("enabled") or er.get("profile") == "base":
        return sp
    pack = PROFILES.get(er["profile"]) or {}
    for section, knobs in pack.items():
        base = dict(sp.get(section) or {})
        base.update(knobs)
        sp[section] = base
    return sp
