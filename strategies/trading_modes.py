"""Resolve portfolio/coin trading mode from regime allocation + vol tier (Phase A/B)."""

from __future__ import annotations

from typing import Any

MODE_GRID = "GRID"
MODE_MOMENTUM = "MOMENTUM"
MODE_HYBRID = "HYBRID"
MODE_DEFENSIVE = "DEFENSIVE"

VALID_MODES = frozenset({MODE_GRID, MODE_MOMENTUM, MODE_HYBRID, MODE_DEFENSIVE})


def _weights_from_allocation(allocation: Any) -> dict[str, float]:
    if allocation is None:
        return {"grid": 0.0, "momentum": 1.0}
    if isinstance(allocation, dict):
        sw = allocation.get("strategy_weights") or allocation
        if isinstance(sw, dict):
            return {
                "grid": float(sw.get("grid", 0) or 0),
                "momentum": float(sw.get("momentum", 0) or 0),
            }
        return {"grid": 0.0, "momentum": 1.0}
    sw = getattr(allocation, "strategy_weights", None) or {}
    if isinstance(sw, dict):
        return {
            "grid": float(sw.get("grid", 0) or 0),
            "momentum": float(sw.get("momentum", 0) or 0),
        }
    return {"grid": 0.0, "momentum": 1.0}


def _defensive_from_allocation(allocation: Any) -> bool:
    if allocation is None:
        return False
    if isinstance(allocation, dict):
        return bool(allocation.get("defensive_mode", False))
    return bool(getattr(allocation, "defensive_mode", False))


def resolve_trading_mode(
    allocation: Any = None,
    *,
    force_grid: bool = False,
    volatility_tier: str = "",
    coin_class: str = "",
    grid_min: float = 0.45,
    hybrid_min: float = 0.32,
) -> str:
    """Map allocator weights + coin tier → a single trading mode.

    Uses existing volatile/stable split:
    - *volatile* / meme: prefer HYBRID when weights are mixed (grid + momentum both useful)
    - *stable* / large_cap: pure GRID when ranging (tighter, cleaner rotation)
    """
    if force_grid:
        return MODE_GRID
    if _defensive_from_allocation(allocation):
        return MODE_DEFENSIVE

    w = _weights_from_allocation(allocation)
    g = w.get("grid", 0.0)
    m = w.get("momentum", 0.0)
    tier = (volatility_tier or "").strip().lower()
    cls = (coin_class or "").strip().lower()
    is_volatile = tier == "volatile" or cls in ("meme", "micro")
    is_stable = tier == "stable" or cls in ("large_cap", "large", "bluechip")

    # Volatile: need higher grid dominance for pure GRID (spikes matter → HYBRID)
    g_min = 0.55 if is_volatile else grid_min
    h_min = 0.28 if is_volatile else hybrid_min

    if g >= g_min and g > m:
        # Stable coins in clear grid regime → GRID; volatile still often HYBRID
        if is_volatile and m >= 0.25:
            return MODE_HYBRID
        return MODE_GRID
    if g >= h_min and m >= h_min:
        return MODE_HYBRID
    if is_stable and g >= 0.40 and g >= m:
        return MODE_GRID
    return MODE_MOMENTUM


def mode_allows_entry_sensor_full_size(mode: str) -> bool:
    """Full entry-sensor size only outside pure GRID mode."""
    return mode in (MODE_MOMENTUM, MODE_HYBRID)


def mode_allows_new_grid_buys(mode: str) -> bool:
    return mode in (MODE_GRID, MODE_HYBRID)


def mode_allows_grid_sells(mode: str) -> bool:
    return mode in (MODE_GRID, MODE_HYBRID, MODE_DEFENSIVE)


def entry_sensor_buy_usdt_frac(mode: str, *, volatility_tier: str = "") -> float:
    """Fraction of max_usdt_per_trade when entry sensor lifts a buy.

    GRID: small slice (rotation-friendly). HYBRID: medium. MOMENTUM: full (1.0).
    Volatile GRID slices a bit larger than stable GRID.
    """
    tier = (volatility_tier or "").lower()
    if mode == MODE_GRID:
        return 0.35 if tier == "volatile" else 0.22
    if mode == MODE_HYBRID:
        return 0.55 if tier == "volatile" else 0.40
    return 1.0
