"""Resolve a stable exit_source label for sell orders (ledger observability).

`source` on orders remains the channel (auto/grid/dca/manual/…).
`exit_source` is the strategy/module that actually drove the sell.
"""

from __future__ import annotations

from strategies.sell_sources import (
    SOCIAL_SOURCES,
    STOP_SOURCES,
    STRUCTURE_SOURCES,
    TRAILING_SOURCES,
)

# Prefer more specific exit modules over generic channel tags
_EXIT_PRIORITY: tuple[str, ...] = (
    "slot_evict_for_entry",
    "ladder_terminal",
    "tail_idle",
    "trailing_stop",
    "trailing_take_profit",
    "profit_max_lifetime",
    "time_profit_exit",
    "x_stop_loss",
    "stop_loss",
    "x_take_profit",
    "exit_15m_weakness",
    "exit_volume_climax",
    "exit_pullback",
    "exit_btc_rs",
    "exit_1h_rsi_rollover",
    "bb_upper",
    "vol_exhaustion",
    "vol_dump",
    "technical",
    "grid",
    "cmc",
    "lc",
    "x",
)

_CHANNEL_TAGS = frozenset(
    {
        "auto",
        "manual",
        "hermes",
        "multi_source",
        "sell_policy_shadow",
        "entry_sensor_shadow",
        "exit_sensor_shadow",
        "trailing_shadow",
        "trailing_take_profit_shadow",
        "profit_max_lifetime_shadow",
        "time_profit_shadow",
        "dca_shadow",
        "dca_recovery_shadow",
        "dca_scheduled_shadow",
        "dca_portfolio_deferred",
        "coin_filter_blocked",
        "grid_slice",
        "hybrid_slice",
        "sensor_size_cap",
    }
)


def resolve_exit_source(
    *,
    sell_source: str = "",
    sources: list[str] | None = None,
    action: str = "",
) -> str:
    """Pick the best single label for why we sold."""
    ss = str(sell_source or "").strip()
    if ss and ss not in _CHANNEL_TAGS:
        return ss

    srcs = [str(s).strip() for s in (sources or []) if str(s).strip()]
    # prefer priority list order
    src_set = set(srcs)
    for key in _EXIT_PRIORITY:
        if key in src_set:
            return key

    for s in srcs:
        if s in _CHANNEL_TAGS:
            continue
        if s in STRUCTURE_SOURCES or s in TRAILING_SOURCES or s in STOP_SOURCES or s in SOCIAL_SOURCES:
            return s
        if s and not s.endswith("_shadow"):
            return s

    act = str(action or "").upper()
    if "STOP" in act:
        return "stop_loss"
    if "PARTIAL" in act or "FULL" in act or act.startswith("SELL"):
        return "unknown_sell"
    return ""


def truncate_rationale(text: str, *, max_len: int = 240) -> str:
    t = " ".join(str(text or "").split())
    if len(t) <= max_len:
        return t
    return t[: max_len - 1] + "…"
