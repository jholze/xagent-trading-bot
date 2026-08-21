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


def compute_stop_price(
    entry: float,
    recent_high: float,
    trail_pct: float,
    *,
    floor_at_entry: bool = True,
    be_buffer_pct: float = 0.0,
) -> float:
    """Stop level from peak trail, never below entry floor when floor_at_entry.

    LAB-class: peak +19%, trail 10% → stop ~+7% vs entry (not free fall to -5%).
    Continuous eval must sell near this level; late eval can still fill worse.
    """
    if recent_high <= 0 or entry <= 0:
        return entry
    raw = recent_high * (1.0 - float(trail_pct) / 100.0)
    if not floor_at_entry:
        return raw
    floor = entry * (1.0 + float(be_buffer_pct) / 100.0)
    return max(floor, raw)


def evaluate_trailing_stop(
    market: MarketContext,
    position: dict,
    strategy_params: dict | None,
    *,
    now=None,
    climax_decision=None,
    config_raw: dict | None = None,
) -> TrailingStopCandidate | None:
    """Full SELL when price hits stop after arm.

    Arm uses **peak gain** (default) so dumps do not disarm the stop.
    Stop = max(entry floor, peak × (1 − trail%)) so after a real peak the
    stop sits **above entry** — not a free ride back to −5% (LAB).

    If price has already crashed *through* the floor (``floor_breach_pct``,
    default 1%), skip: paper would fill at the crashed print (HANA −32%).
    Hard SL and DCA own that zone.

    After DCA, trail exits are paused for a grace window (see strategies.dca).
    """
    cfg = trailing_config(strategy_params)
    if not cfg or not cfg.get("enabled", True):
        return None
    if not market.has_position or market.average_entry <= 0:
        return None

    try:
        from strategies.oracle_climax import climax_ttp_adjust

        _cfg, skip = climax_ttp_adjust(
            {}, config_raw=config_raw, climax_decision=climax_decision
        )
        if skip:
            return None
    except Exception:
        pass

    try:
        from strategies.dca import trail_exits_paused_after_dca

        paused, _why = trail_exits_paused_after_dca(
            position, strategy_params, now=now
        )
        if paused:
            return None
    except Exception:
        pass

    try:
        from strategies.recovery_hold import (
            auto_sells_blocked_reason,
            maybe_promote_recovery_hold,
        )

        # BE+ may clear hold before trail eval (persist only when cleared)
        if position is not None and market.current_price > 0:
            if maybe_promote_recovery_hold(
                position, market.current_price, strategy_params=strategy_params
            ):
                try:
                    from strategies.positions import flush_positions

                    flush_positions()
                except Exception:
                    pass
        if auto_sells_blocked_reason(
            position, "trailing_stop", strategy_params=strategy_params
        ):
            return None
    except Exception:
        pass

    entry = market.average_entry
    price = market.current_price
    if price <= 0 or entry <= 0:
        return None

    gain_pct = (price / entry - 1.0) * 100.0
    # Peak for trail: ledger recent_high (reanchor + stamp_peak_epoch_on_dca fix stale peaks)
    # Do NOT clamp to peak_epoch_high on every tick — that would ignore post-DCA run-ups.
    recent_high = float((position or {}).get("recent_high") or 0) or price
    if recent_high <= 0:
        return None
    if recent_high < price:
        recent_high = price
    peak_gain_pct = (recent_high / entry - 1.0) * 100.0

    activation = float(cfg.get("activation_gain_pct", 10.0))
    arm_on_peak = cfg.get("arm_on_peak", True)
    if arm_on_peak:
        if peak_gain_pct < activation:
            return None
    else:
        if gain_pct < activation:
            return None

    trail_pct = compute_trail_pct(market.atr_pct, cfg)
    # Cap trail so theoretical stop is not forced miles under entry by huge ATR
    # (stop floor still applies; this keeps drop-distance sane vs peak height).
    be_buffer = float(cfg.get("be_buffer_pct") or 0.0)
    floor_at_entry = cfg.get("floor_at_entry", True)
    if floor_at_entry and peak_gain_pct > 0:
        # trail cannot exceed peak_gain − buffer or stop would sit under floor anyway
        trail_pct = min(trail_pct, max(peak_gain_pct - be_buffer, 0.0))

    stop_px = compute_stop_price(
        entry,
        recent_high,
        trail_pct,
        floor_at_entry=floor_at_entry,
        be_buffer_pct=be_buffer,
    )
    drop_pct = (1.0 - price / recent_high) * 100.0 if recent_high > 0 else 0.0

    if price > stop_px:
        return None

    # floor_at_entry: do not market-dump a crash through the floor.
    # Henry HANA 2026-08-20: stop sat at entry (~+0%) but px was already -32%;
    # paper filled at the crashed print. Tiny BE wiggle stays a trail fire.
    if floor_at_entry:
        breach_pct = float(cfg.get("floor_breach_pct") or 1.0)
        if gain_pct < -abs(breach_pct):
            return None

    mode = str(cfg.get("mode", "live")).strip().lower()
    shadow = mode == "shadow"
    stop_gain = (stop_px / entry - 1.0) * 100.0
    why = (
        f"Trail->stop (px {price:.6g} <= stop {stop_px:.6g} "
        f"[~{stop_gain:+.1f}% vs entry], drop {drop_pct:.1f}%, "
        f"trail {trail_pct:.1f}%, peak {peak_gain_pct:.1f}%)"
    )
    return TrailingStopCandidate(
        action=SELL_FULL,
        source="trailing_stop",
        priority=6,
        rationale=why,
        shadow_only=shadow,
    )
