"""Read-side reporting helpers (slippage, attribution, live metrics)."""

from __future__ import annotations


def clamp_days(days: object, default: int = 7, *, lo: int = 1, hi: int = 365) -> int:
    """Clamp a lookback window in days. Invalid values fall back to *default*."""
    try:
        n = int(days)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        n = int(default)
    return max(int(lo), min(int(hi), n))
