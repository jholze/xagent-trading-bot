"""Position gain/peak/trail metrics for observability."""

from __future__ import annotations

from core.models import MarketContext
from strategies.sell_rotation_policy import trail_replacement_armed
from strategies.trailing_take_profit import resolve_trail_pct, trailing_take_profit_config


def position_metrics(
    market: MarketContext,
    position: dict,
    strategy_params: dict | None,
) -> dict:
    entry = float(market.average_entry or 0)
    price = float(market.current_price or 0)
    recent_high = float(position.get("recent_high") or 0) or price

    gain_pct = ((price / entry) - 1) * 100 if entry > 0 else 0.0
    peak_gain_pct = ((recent_high / entry) - 1) * 100 if entry > 0 else 0.0
    drop_from_high_pct = (
        (1 - price / recent_high) * 100 if recent_high > 0 else 0.0
    )

    ttp_cfg = trailing_take_profit_config(strategy_params)
    trail_pct_resolved = None
    if ttp_cfg.get("enabled", False):
        trail_pct_resolved = round(resolve_trail_pct(peak_gain_pct, ttp_cfg), 2)

    return {
        "has_position": bool(market.has_position and entry > 0),
        "gain_pct": round(gain_pct, 2),
        "peak_gain_pct": round(peak_gain_pct, 2),
        "recent_high": recent_high,
        "drop_from_high_pct": round(drop_from_high_pct, 2),
        "trail_armed": bool(
            trail_replacement_armed(strategy_params, market, position)
        ),
        "trail_pct_resolved": trail_pct_resolved,
        "strategy_profile": (strategy_params or {}).get("strategy_profile", ""),
        "volatility_tier": (strategy_params or {}).get("volatility_tier", "")
        or position.get("strategy_tier", ""),
        "exit_ladder_step": int(position.get("exit_ladder_step", 0) or 0),
    }