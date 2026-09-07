"""Position gain/peak/trail metrics for observability."""

from __future__ import annotations

from datetime import datetime

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




def _held_label(first_buy_at: object, now: datetime | None = None) -> str | None:
    # first_buy_at is written naive in the writer's process clock (UTC on
    # Railway). Compare in aware UTC like order_service does since #320 —
    # a Berlin "now" against a naive UTC stamp would show 2 h too much.
    from core.time_utils import ledger_datetime_utc, to_utc, utc_now

    start = ledger_datetime_utc(first_buy_at)
    if start is None:
        return None
    now_utc = utc_now() if now is None else to_utc(now)
    delta = now_utc - start
    if delta.total_seconds() < 0:
        return None
    days = int(delta.days)
    hours = int(delta.seconds // 3600)
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h"
    mins = int(delta.seconds // 60)
    return f"{mins}m"


def _stop_price_label(price: float) -> str:
    if price >= 1:
        return f"{price:.2f}"
    if price >= 0.01:
        return f"{price:.4f}".rstrip("0").rstrip(".")
    return f"{price:.6g}"


def _ladder_n(position: dict, strategy_params: dict | None) -> int:
    params = strategy_params or {}
    el = params.get("exit_ladder") or {}
    tiers = el.get("tiers") if isinstance(el, dict) else None
    if isinstance(tiers, list) and tiers:
        return len(tiers)
    try:
        from strategies.exit_ladder import default_ladder_tiers

        return len(default_ladder_tiers() or [])
    except Exception:
        return 0


def format_exit_plan_line(
    position: dict,
    price: float,
    strategy_params: dict | None = None,
    *,
    now: datetime | None = None,
) -> str:
    """Compact exit-plan line, or '' when none of the fields are available.

    Example: ``Stop -8% ($0.92) · Ladder 2/3 · Trail armed ✓ · Peak +14% (-5% off) · Held 3d 4h``
    """
    if not isinstance(position, dict):
        return ""
    params = strategy_params or {}
    entry = float(position.get("average_entry") or position.get("entry_price") or 0)
    try:
        px = float(price or 0)
    except (TypeError, ValueError):
        px = 0.0
    market = MarketContext(
        symbol=str(position.get("symbol") or ""),
        timeframe=str(position.get("timeframe") or "4h"),
        current_price=px if px > 0 else entry,
        has_position=entry > 0,
        average_entry=entry,
    )
    try:
        metrics = position_metrics(market, position, params)
    except Exception:
        metrics = {}

    bits: list[str] = []

    stop_pct = params.get("stop_loss_pct", position.get("stop_loss_pct"))
    try:
        stop_pct_f = float(stop_pct) if stop_pct is not None else None
    except (TypeError, ValueError):
        stop_pct_f = None
    if stop_pct_f and stop_pct_f > 0 and entry > 0:
        stop_px = entry * (1.0 - stop_pct_f / 100.0)
        pct_s = f"{stop_pct_f:.0f}" if abs(stop_pct_f - round(stop_pct_f)) < 1e-9 else f"{stop_pct_f:g}"
        bits.append(f"Stop -{pct_s}% (${_stop_price_label(stop_px)})")

    step = int(metrics.get("exit_ladder_step") or 0)
    if "exit_ladder_step" in position or step > 0:
        n = _ladder_n(position, params)
        if n <= 0:
            n = max(step, 1)
        bits.append(f"Ladder {step}/{n}")

    if metrics.get("trail_armed"):
        bits.append("Trail armed ✓")
    elif metrics.get("trail_pct_resolved") is not None:
        bits.append(f"Trail {metrics['trail_pct_resolved']:g}%")

    if position.get("recent_high"):
        peak = float(metrics.get("peak_gain_pct") or 0)
        drop = float(metrics.get("drop_from_high_pct") or 0)
        bits.append(f"Peak {peak:+.0f}% (-{drop:.0f}% off)")

    held = _held_label(position.get("first_buy_at") or position.get("entry_at"), now=now)
    if held:
        bits.append(f"Held {held}")

    return " · ".join(bits)