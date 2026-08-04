"""Fixed-percent trailing take-profit — sell sizing via exit ladder."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from core.actions import SELL_FULL, SELL_PARTIAL_30
from core.models import MarketContext
from strategies.exit_ladder import current_ladder_step, ladder_config, ladder_enabled


@dataclass
class TrailingTakeProfitCandidate:
    action: str
    source: str
    priority: int
    rationale: str
    shadow_only: bool = False


def trailing_take_profit_config(strategy_params: dict | None) -> dict:
    cfg = dict((strategy_params or {}).get("trailing_take_profit") or {})
    try:
        from services.exit_rotation import apply_exit_section_overlay

        cfg = apply_exit_section_overlay(cfg, "trailing_take_profit")
    except Exception:
        pass
    return cfg


def _gain_pct(market: MarketContext) -> float:
    entry = market.average_entry
    if entry <= 0:
        return 0.0
    return (market.current_price / entry - 1) * 100


def _peak_gain_pct(market: MarketContext, position: dict) -> float:
    entry = market.average_entry
    if entry <= 0:
        return 0.0
    recent_high = float(position.get("recent_high") or 0) or market.current_price
    return (recent_high / entry - 1) * 100


def resolve_trail_pct(peak_gain_pct: float, cfg: dict) -> float:
    """Scale trail width with peak gain: tight after arm, wider on big runners."""
    if not cfg.get("dynamic_trail", True):
        return float(cfg.get("trail_pct", 6.0))

    lo = float(cfg.get("trail_pct_min", 3.0))
    hi = float(cfg.get("trail_pct_max", 12.0))
    scale_start = float(cfg.get("trail_pct_scale_start_pct", 18.0))
    scale_peak = float(cfg.get("trail_pct_scale_peak_pct", 45.0))

    if peak_gain_pct <= scale_start:
        return lo
    if peak_gain_pct >= scale_peak:
        return hi
    if scale_peak <= scale_start:
        return hi

    t = (peak_gain_pct - scale_start) / (scale_peak - scale_start)
    return lo + t * (hi - lo)


def _hours_since(iso_ts: str | None, now: datetime) -> float | None:
    if not iso_ts:
        return None
    try:
        last_ts = datetime.fromisoformat(str(iso_ts).replace("Z", ""))
    except Exception:
        return None
    return (now - last_ts).total_seconds() / 3600.0


def _resolve_action(position: dict, strategy_params: dict | None) -> str | None:
    """Resolve TTP action. max_steps=1 (config default) → always full close."""
    if ladder_enabled(strategy_params):
        tiers = ladder_config(strategy_params).get("tiers") or []
        if not tiers:
            return None
        step = current_ladder_step(position, tiers)
        if step >= len(tiers):
            return SELL_FULL
        if step >= len(tiers) - 1:
            return SELL_FULL
        return SELL_PARTIAL_30

    # Default max_steps=1: one trail hit = full close (no partial tails)
    max_steps = int(trailing_take_profit_config(strategy_params).get("max_steps", 1))
    steps = int(position.get("trail_tp_steps", 0) or 0)
    if steps >= max_steps:
        return None
    return SELL_FULL if max_steps <= 1 or steps >= max_steps - 1 else SELL_PARTIAL_30


def evaluate_trailing_take_profit(
    market: MarketContext,
    position: dict,
    strategy_params: dict | None,
    *,
    now: datetime | None = None,
) -> TrailingTakeProfitCandidate | None:
    cfg = trailing_take_profit_config(strategy_params)
    if not cfg.get("enabled", False):
        return None
    mode = str(cfg.get("mode", "live")).strip().lower()
    if mode in ("off", "disabled"):
        return None
    if not market.has_position or market.average_entry <= 0:
        return None

    action = _resolve_action(position, strategy_params)
    if not action:
        return None

    peak_gain = _peak_gain_pct(market, position)
    arm_gain = float(cfg.get("arm_gain_pct", 15.0))
    if peak_gain < arm_gain:
        return None

    gain = _gain_pct(market)
    if cfg.get("dynamic_trail", True):
        min_gain = float(cfg.get("min_gain_pct_floor", 8.0))
    else:
        min_gain = float(cfg.get("min_gain_pct", 10.0))
    # Hard floor: TTP never sells at a loss (TS handles dump-to-underwater).
    if gain < 0:
        return None
    # Soft min_gain: after a real peak, allow trail exit above 0 even if below
    # min_gain — captures giveback from peak without waiting for min_gain wall.
    allow_soft = bool(cfg.get("trail_above_zero_after_arm", True))
    if gain < min_gain and not (allow_soft and peak_gain >= arm_gain and gain > 0):
        return None

    recent_high = float(position.get("recent_high") or 0) or market.current_price
    if recent_high <= 0:
        return None
    if recent_high < market.current_price:
        recent_high = market.current_price
    drop_pct = (1 - market.current_price / recent_high) * 100
    trail_pct = resolve_trail_pct(peak_gain, cfg)
    if drop_pct < trail_pct:
        return None

    now = now or datetime.now()
    cooldown_h = float(cfg.get("cooldown_hours", 6))
    elapsed = _hours_since(position.get("last_trail_tp_at"), now)
    if elapsed is not None and elapsed < cooldown_h:
        return None

    shadow = mode == "shadow"
    # Higher than trailing_stop (6) so profit-take wins in DE when both fire.
    priority = int(cfg.get("priority", 7))
    return TrailingTakeProfitCandidate(
        action=action,
        source="trailing_take_profit",
        priority=priority,
        rationale=(
            f"TrailTP->{action} (drop {drop_pct:.1f}% from high, "
            f"trail {trail_pct:.1f}%, peak={peak_gain:.1f}%, gain={gain:.1f}%)"
        ),
        shadow_only=shadow,
    )