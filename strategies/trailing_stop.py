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
    """Fire full SELL when drop from peak exceeds trail, after arm.

    Arm uses **peak gain** (recent_high vs entry) by default so a dump back
    toward entry does not disarm the stop (IDOL-class bug: current gain <
    activation after giveback → never sells).

    Optional after-arm floor: if price is back at/below entry (plus buffer),
    exit even when drop < ATR trail — protects full giveback of the run.
    """
    cfg = trailing_config(strategy_params)
    if not cfg or not cfg.get("enabled", True):
        return None
    if not market.has_position or market.average_entry <= 0:
        return None

    entry = market.average_entry
    price = market.current_price
    if price <= 0 or entry <= 0:
        return None

    gain_pct = (price / entry - 1.0) * 100.0
    recent_high = float(position.get("recent_high") or 0) or price
    if recent_high <= 0:
        return None
    if recent_high < price:
        recent_high = price
    peak_gain_pct = (recent_high / entry - 1.0) * 100.0

    activation = float(cfg.get("activation_gain_pct", 10.0))
    # Default: arm on peak (sticky). Legacy: arm only while current gain high.
    arm_on_peak = cfg.get("arm_on_peak", True)
    if arm_on_peak:
        if peak_gain_pct < activation:
            return None
    else:
        if gain_pct < activation:
            return None

    drop_pct = (1.0 - price / recent_high) * 100.0
    trail_pct = compute_trail_pct(market.atr_pct, cfg)

    # Full giveback of the run: price back at entry (or slightly above with buffer)
    be_after_arm = cfg.get("breakeven_exit_after_arm", True)
    be_buffer = float(cfg.get("be_buffer_pct") or 0.0)
    entry_floor = entry * (1.0 + be_buffer / 100.0)
    hit_breakeven = bool(be_after_arm and price <= entry_floor)

    hit_trail = drop_pct >= trail_pct
    if not hit_trail and not hit_breakeven:
        return None

    mode = str(cfg.get("mode", "live")).strip().lower()
    shadow = mode == "shadow"
    if hit_breakeven and not hit_trail:
        why = (
            f"Trail->BE after arm (peak {peak_gain_pct:.1f}%, now {gain_pct:.1f}%, "
            f"drop {drop_pct:.1f}% < trail {trail_pct:.1f}%)"
        )
    else:
        why = (
            f"Trail->ATR stop (drop {drop_pct:.1f}% from high, trail {trail_pct:.1f}%, "
            f"peak {peak_gain_pct:.1f}%)"
        )
    return TrailingStopCandidate(
        action=SELL_FULL,
        source="trailing_stop",
        priority=6,
        rationale=why,
        shadow_only=shadow,
    )
