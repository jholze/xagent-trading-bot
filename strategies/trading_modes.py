"""Resolve portfolio/coin trading mode from regime allocation (Phase A)."""

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
    grid_min: float = 0.45,
    hybrid_min: float = 0.32,
) -> str:
    """Map allocator weights → a single trading mode for this coin/cycle."""
    if force_grid:
        return MODE_GRID
    if _defensive_from_allocation(allocation):
        return MODE_DEFENSIVE

    w = _weights_from_allocation(allocation)
    g = w.get("grid", 0.0)
    m = w.get("momentum", 0.0)

    if g >= grid_min and g > m:
        return MODE_GRID
    if g >= hybrid_min and m >= hybrid_min:
        return MODE_HYBRID
    return MODE_MOMENTUM


def mode_allows_entry_sensor_full_size(mode: str) -> bool:
    """Full entry-sensor size only outside pure GRID mode."""
    return mode in (MODE_MOMENTUM, MODE_HYBRID)


def mode_allows_new_grid_buys(mode: str) -> bool:
    return mode in (MODE_GRID, MODE_HYBRID)


def mode_allows_grid_sells(mode: str) -> bool:
    return mode in (MODE_GRID, MODE_HYBRID, MODE_DEFENSIVE)
