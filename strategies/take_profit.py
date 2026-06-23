"""Gain-based take-profit tier helpers (TA signals + position tier tracking)."""

from __future__ import annotations


def tier_key(level: float | int) -> str:
    return f"tp{int(level)}"


def normalized_tiers(tiers) -> list[float]:
    return sorted(float(t) for t in (tiers or []) if t is not None)


def next_trigger_level(
    gain_pct: float,
    tiers,
    tiers_done: dict | None,
) -> float | None:
    """Lowest configured gain tier that is met and not yet marked done."""
    done = tiers_done or {}
    for level in normalized_tiers(tiers):
        if gain_pct >= level and not done.get(tier_key(level)):
            return level
    return None


def mark_triggered_tier(
    tiers_done: dict | None,
    gain_pct: float,
    tiers,
) -> dict:
    """Mark the highest newly eligible TP tier; fall back to legacy ``tp`` key."""
    updated = dict(tiers_done or {})
    for level in sorted(normalized_tiers(tiers), reverse=True):
        key = tier_key(level)
        if gain_pct >= level and not updated.get(key):
            updated[key] = True
            return updated
    updated["tp"] = True
    return updated