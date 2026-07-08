"""Shared sell-candidate source identifiers for rotation and entry guard."""

from __future__ import annotations

STRUCTURE_SOURCES = frozenset({"bb_upper", "vol_exhaustion", "vol_dump"})

TRAIL_ROTATION_SOURCES = frozenset({
    "trailing_take_profit",
    "profit_max_lifetime",
    "time_profit_exit",
})

TRAILING_SOURCES = TRAIL_ROTATION_SOURCES | frozenset({"trailing_stop"})

TRAIL_EXCLUSIVE_BLOCK_SOURCES = STRUCTURE_SOURCES | frozenset({
    "technical", "cmc", "lc", "x", "x_take_profit", "rsi",
})

SOCIAL_SOURCES = frozenset({"cmc", "lc", "x", "x_take_profit"})

STOP_SOURCES = frozenset({"x_stop_loss", "stop_loss", "technical"})