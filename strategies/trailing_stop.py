"""ATR-scaled trailing stop for volatile profiles (gain protection)."""

from __future__ import annotations

from dataclasses import dataclass

from core.actions import SELL_FULL
from core.models import MarketContext


@dataclass
class TrailingStopCandidate:
    action: str
    source: str
    priority: int
    rationale: str
    shadow_only: bool = False


def trailing_config(strategy_params: dict | None) -> dict:
    """Return trailing-stop config when the resolved profile includes it."""
    params = strategy_params or {}
    return dict(params.get("trailing_stop") or {})


def trailing_enabled(strategy_params: dict | None) -> bool:
    cfg = trailing_config(strategy_params)
    return bool(cfg.get("enabled", True))


def compute_trail_pct(atr_pct: float, cfg: dict) -> float:
    mult = float(cfg.get("atr_multiplier", 2.0))
    lo = float(cfg.get("min_trail_pct", 8.0))
    hi = float(cfg.get("max_trail_pct", 25.0))
    raw = float(atr_pct or 3.0) * mult
    return max(lo, min(hi, raw))


def evaluate_trailing_stop(
    market: MarketContext,
    position: dict,
    strategy_params: dict | None,
) -> TrailingStopCandidate | None:
    cfg = trailing_config(strategy_params)
    if not cfg or not cfg.get("enabled", True):
        return None
    if not market.has_position or market.average_entry <= 0:
        return None

    entry = market.average_entry
    price = market.current_price
    gain_pct = (price / entry - 1) * 100
    activation = float(cfg.get("activation_gain_pct", 10.0))
    if gain_pct < activation:
        return None

    recent_high = float(position.get("recent_high") or 0) or price
    if recent_high <= 0:
        return None
    drop_pct = (1 - price / recent_high) * 100
    trail_pct = compute_trail_pct(market.atr_pct, cfg)
    if drop_pct < trail_pct:
        return None

    mode = str(cfg.get("mode", "live")).strip().lower()
    shadow = mode == "shadow"
    return TrailingStopCandidate(
        action=SELL_FULL,
        source="trailing_stop",
        priority=6,
        rationale=f"Trail->ATR stop (drop {drop_pct:.1f}% from high, trail {trail_pct:.1f}%)",
        shadow_only=shadow,
    )