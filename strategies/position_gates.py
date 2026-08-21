"""Central exit / DCA permission checks (lock + recovery_hold).

Keeps board, risk, sniper, and exit_ws from inventing divergent rules.
"""

from __future__ import annotations

from typing import Any

# Trail-class sources blocked under recovery_hold (hard SL may still fire)
HOLD_BLOCKABLE_SOURCES = frozenset(
    {
        "trailing_take_profit",
        "trailing_stop",
        "partial_stop",
        "profit_max_lifetime",
        "oracle_climax_harvest",
        "safety_tp",
        "exit_ws",
        "auto",
        "dca_sniper_fund",
    }
)

_MANUAL = frozenset(
    {"manual", "telegram", "user", "operator", "confirm", "manual_order", "manual_sell"}
)
_HARD_SL = frozenset({"stop_loss", "hard_sl"})


def is_recovery_hold(pos: dict | None) -> bool:
    if not isinstance(pos, dict):
        return False
    try:
        from strategies.recovery_hold import is_recovery_hold_active

        return bool(is_recovery_hold_active(pos))
    except Exception:
        return bool(pos.get("recovery_hold") or pos.get("sniper_focus"))


def auto_exit_blocked(
    pos: dict | None,
    source: str | None = None,
    *,
    config: dict | None = None,
) -> tuple[bool, str]:
    """True if auto sell must not execute (recovery_hold and/or position lock)."""
    src = str(source or "").strip().lower()
    if is_recovery_hold(pos):
        if src in _MANUAL or src in _HARD_SL:
            pass  # allow manual / hard SL past hold
        elif not src or src in HOLD_BLOCKABLE_SOURCES or src.startswith("tp_tier"):
            return True, "recovery_hold"
        else:
            # other auto sources under hold
            if src not in _MANUAL and src not in _HARD_SL:
                return True, "recovery_hold"
    try:
        from strategies.position_lock import auto_sell_blocked

        return auto_sell_blocked(pos, source, config=config)
    except Exception:
        return False, ""


def dca_add_blocked(
    pos: dict | None,
    *,
    config: dict | None = None,
) -> tuple[bool, str]:
    """True if DCA / sniper add-on must not execute (explicit no_dca only)."""
    try:
        from strategies.position_lock import dca_blocked

        return dca_blocked(pos, config=config)
    except Exception:
        return False, ""


def filter_would_sources_for_hold(
    would_sources: list[str],
    *,
    recovery_hold: bool,
) -> tuple[list[str], list[str]]:
    """Return (allowed_would, blocked_by_hold) for board eval parity."""
    if not recovery_hold:
        return list(would_sources), []
    blocked: list[str] = []
    allowed: list[str] = []
    for s in would_sources:
        if s in HOLD_BLOCKABLE_SOURCES or str(s).startswith("tp_tier_"):
            blocked.append(s)
        else:
            allowed.append(s)
    return allowed, blocked
