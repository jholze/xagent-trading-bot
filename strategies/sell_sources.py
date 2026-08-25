"""Shared sell-candidate source identifiers for rotation and entry guard."""

from __future__ import annotations

STRUCTURE_SOURCES = frozenset({
    "bb_upper",
    "vol_exhaustion",
    "vol_dump",
    "exit_15m_weakness",
    "exit_volume_climax",
    "exit_pullback",
    "exit_btc_rs",
    "exit_1h_rsi_rollover",
})

TRAIL_ROTATION_SOURCES = frozenset({
    "trailing_take_profit",
    "profit_max_lifetime",
    "time_profit_exit",
})

TRAILING_SOURCES = TRAIL_ROTATION_SOURCES | frozenset({"trailing_stop"})

RSI_SELL_SOURCE = "rsi_sell"

# Punch through trail_exclusive when sell_policy.indicator_regime.trail_allow_rsi.
# Do NOT add generic "technical" — that would also free take_profit / mixed TA.
TRAIL_ALLOW_RSI_SOURCES = frozenset({
    RSI_SELL_SOURCE,
    "exit_1h_rsi_rollover",
})

TRAIL_EXCLUSIVE_BLOCK_SOURCES = STRUCTURE_SOURCES | frozenset({
    "technical", "cmc", "lc", "x", "x_take_profit", "rsi",
    RSI_SELL_SOURCE,
})

SOCIAL_SOURCES = frozenset({"cmc", "lc", "x", "x_take_profit"})

STOP_SOURCES = frozenset({"x_stop_loss", "stop_loss", "partial_stop"})

# Portfolio hygiene: free a full slot for high-conviction new entry (#111)
SLOT_EVICT_SOURCES = frozenset({"slot_evict_for_entry"})

# Oracle RISK_ON climax dump harvest (sell_policy.oracle_climax)
CLIMAX_HARVEST_SOURCE = "oracle_climax_harvest"