"""Mode-aware entry-sensor hold_override + size_hint (sensor-entry-guard M1).

Pure helpers — DecisionEngine / Risk call these without I/O.
"""

from __future__ import annotations

from typing import Any

from strategies.trading_modes import (
    MODE_DEFENSIVE,
    MODE_GRID,
    MODE_HYBRID,
    MODE_MOMENTUM,
    entry_sensor_buy_usdt_frac,
)

SENSOR_SOURCES = frozenset(
    {"entry_sensor_15m", "vol_spike_15m", "entry_sensor", "15m_sensor"}
)

# Defaults match master plan staging recommendations
_DEFAULT_HOLD_OVERRIDE_BY_MODE = {
    MODE_GRID: "slice_only",
    MODE_HYBRID: "allow_with_conditions",
    MODE_MOMENTUM: "block",
    MODE_DEFENSIVE: "off",
    "": "block",
}


def is_sensor_source(source: str | None) -> bool:
    s = (source or "").lower()
    if s in SENSOR_SOURCES:
        return True
    return "entry_sensor" in s or s.startswith("vol_spike")


def hold_override_mode(trading_mode: str, cfg: dict | None = None) -> str:
    cfg = cfg or {}
    mode_u = (trading_mode or "").strip().upper()
    # No resolved trading_mode (regime off / not yet set): keep pre-guard behavior
    # so HOLD→sensor BUY still works until allocator sets GRID|HYBRID|MOMENTUM.
    if not mode_u:
        ho0 = cfg.get("hold_override")
        if isinstance(ho0, dict) and ho0.get("mode"):
            return str(ho0["mode"]).lower()
        if isinstance(ho0, str) and ho0:
            return ho0.lower()
        return "legacy"
    by_mode = cfg.get("hold_override_by_mode")
    if isinstance(by_mode, dict) and by_mode:
        if mode_u in by_mode:
            return str(by_mode[mode_u]).lower()
        if trading_mode in by_mode:
            return str(by_mode[trading_mode]).lower()
    # legacy single mode
    ho = cfg.get("hold_override")
    if isinstance(ho, dict) and ho.get("mode"):
        return str(ho["mode"]).lower()
    if isinstance(ho, str):
        return ho.lower()
    return _DEFAULT_HOLD_OVERRIDE_BY_MODE.get(mode_u, "block")


def resolve_sensor_size_hint(
    trading_mode: str,
    *,
    volatility_tier: str = "",
    cfg: dict | None = None,
) -> float:
    """Fraction of max_usdt_per_trade for sensor buys (0–1)."""
    cfg = cfg or {}
    mode = (trading_mode or "").upper()
    tier = (volatility_tier or "").lower()
    by_mode = cfg.get("size_hint_by_mode")
    if isinstance(by_mode, dict):
        entry = by_mode.get(mode) or by_mode.get(mode.lower())
        if isinstance(entry, dict):
            if tier and entry.get(tier) is not None:
                return max(0.0, min(1.0, float(entry[tier])))
            if entry.get("default") is not None:
                return max(0.0, min(1.0, float(entry["default"])))
            if entry.get("max") is not None and mode == MODE_MOMENTUM:
                return max(0.0, min(1.0, float(entry["max"])))
        elif entry is not None:
            return max(0.0, min(1.0, float(entry)))

    # Prefer trading_modes fracs; MOMENTUM capped via cfg absolute later
    if mode == MODE_MOMENTUM:
        mom = 0.30
        if isinstance(by_mode, dict):
            me = by_mode.get("MOMENTUM") or {}
            if isinstance(me, dict) and me.get("default") is not None:
                mom = float(me["default"])
        return max(0.0, min(1.0, mom))
    return max(0.0, min(1.0, entry_sensor_buy_usdt_frac(mode, volatility_tier=tier)))


def resolve_sensor_usdt(
    trading_mode: str,
    *,
    volatility_tier: str = "",
    max_usdt_per_trade: float = 2500.0,
    cfg: dict | None = None,
) -> float:
    """Absolute USDT for sensor buy after mode frac + absolute cap."""
    cfg = cfg or {}
    hint = resolve_sensor_size_hint(trading_mode, volatility_tier=volatility_tier, cfg=cfg)
    base = float(max_usdt_per_trade or 0) * hint
    abs_cap = cfg.get("max_usdt_absolute")
    if abs_cap is not None and float(abs_cap) > 0:
        base = min(base, float(abs_cap))
    frac_cap = cfg.get("max_usdt_frac_of_max_trade")
    if frac_cap is not None and float(frac_cap) > 0:
        base = min(base, float(max_usdt_per_trade or 0) * float(frac_cap))
    return max(0.0, round(base, 2))


def should_block_hold_override(
    *,
    tech_is_hold: bool,
    trading_mode: str,
    cfg: dict | None = None,
    tech_already_buy: bool = False,
) -> tuple[bool, str]:
    """
    Returns (block, reason).
    When tech is already BUY, never block (sensor boosts only).
    """
    if tech_already_buy or not tech_is_hold:
        return False, ""
    mode = hold_override_mode(trading_mode, cfg)
    if mode in ("legacy", "allow", "off"):
        if mode == "off":
            return True, "hold_override=off (sensor disabled for mode)"
        return False, ""
    if mode == "block":
        return True, f"hold_override=block (TA HOLD, mode={trading_mode or 'MOMENTUM'})"
    if mode == "shadow":
        return True, f"hold_override=shadow (TA HOLD, mode={trading_mode or '?'})"
    # slice_only / allow_with_conditions: allow trigger; size path handles slice
    if mode in ("slice_only", "allow_with_conditions"):
        return False, ""
    # unknown → safe block
    return True, f"hold_override={mode} (safe block)"


def apply_sensor_hold_policy(
    *,
    tech_normalized: str,
    trading_mode: str,
    cfg: dict | None,
    sensor_action: str,
    tech_already_buy: bool,
) -> tuple[str | None, str]:
    """
    If sensor should not produce executable buy from HOLD, return (None, reason).
    If allowed, return (action, "").
    """
    from core.actions import HOLD, is_buy, normalize

    tn = normalize(tech_normalized)
    block, reason = should_block_hold_override(
        tech_is_hold=tn == HOLD or tn == "HOLD",
        trading_mode=trading_mode,
        cfg=cfg,
        tech_already_buy=tech_already_buy or is_buy(tn),
    )
    if block:
        return None, reason
    return sensor_action, ""
