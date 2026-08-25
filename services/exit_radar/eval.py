"""Pure exit-proximity evaluation (viz + radar snapshot)."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def hours_since(iso_ts: str | None, now: datetime | None = None) -> float | None:
    if not iso_ts:
        return None
    try:
        last_ts = datetime.fromisoformat(str(iso_ts).replace("Z", ""))
        if last_ts.tzinfo is not None:
            last_ts = last_ts.replace(tzinfo=None)
    except Exception:
        return None
    now = now or datetime.now()
    return (now - last_ts).total_seconds() / 3600.0





def resolve_trail_pct(peak_gain_pct: float, ttp: dict) -> float:
    if not ttp.get("dynamic_trail", True):
        return float(ttp.get("trail_pct", 6.0))
    lo = float(ttp.get("trail_pct_min", 3.0))
    hi = float(ttp.get("trail_pct_max", 12.0))
    scale_start = float(ttp.get("trail_pct_scale_start_pct", 18.0))
    scale_peak = float(ttp.get("trail_pct_scale_peak_pct", 45.0))
    if peak_gain_pct <= scale_start:
        return lo
    if peak_gain_pct >= scale_peak:
        return hi
    if scale_peak <= scale_start:
        return hi
    t = (peak_gain_pct - scale_start) / (scale_peak - scale_start)
    return lo + t * (hi - lo)





def evaluate_position(
    pos: dict[str, Any],
    price: float,
    *,
    atr_pct_est: float = 5.0,
) -> dict[str, Any]:
    """Pure exit-proximity evaluation against live mark price (viz only)."""
    entry = float(pos["entry"])
    if entry <= 0 or price <= 0:
        return {"symbol": pos["symbol"], "ok": False}

    # Live peak tracking for viz (starts from ledger recent_high or max(entry, price))
    ledger_high = float(pos.get("recent_high") or 0)
    live_high = float(pos.get("_live_high") or 0)
    epoch = float(pos.get("peak_epoch_high") or 0)
    recovery_hold_flag = bool(pos.get("recovery_hold") or pos.get("sniper_focus"))
    # Under recovery_hold: prefer post-DCA epoch peak (never pre-dump lifetime high)
    if recovery_hold_flag and epoch > 0:
        recent_high = max(epoch, price, live_high if live_high and live_high <= epoch * 1.5 else 0)
        if recent_high <= 0:
            recent_high = max(epoch, price)
    else:
        recent_high = max(ledger_high, live_high, entry, price)
        if epoch > 0:
            recent_high = max(recent_high, epoch)
    pos["_live_high"] = recent_high

    gain = (price / entry - 1.0) * 100.0
    peak_gain = (recent_high / entry - 1.0) * 100.0
    drop_from_high = (1.0 - price / recent_high) * 100.0 if recent_high > 0 else 0.0

    ttp = pos["ttp"]
    ts = pos["trailing_stop"]
    life = pos["life"]
    sl_pct = float(pos["stop_loss_pct"])
    partial_sl = float(pos["partial_stop_pct"])

    # --- Trailing take-profit ---
    ttp_trail = resolve_trail_pct(peak_gain, ttp)
    ttp_armed = bool(ttp.get("enabled")) and peak_gain >= float(ttp["arm_gain_pct"])
    ttp_gain_ok = gain >= float(ttp["min_gain_pct"])
    ttp_drop_needed = ttp_trail
    ttp_room = ttp_trail - drop_from_high  # >0 still room; ≤0 would fire
    ttp_fire_price = recent_high * (1.0 - ttp_trail / 100.0) if recent_high else 0.0
    ttp_would = (
        bool(ttp.get("enabled"))
        and ttp_armed
        and ttp_gain_ok
        and drop_from_high >= ttp_trail
    )
    ttp_near = (
        ttp_armed
        and ttp_gain_ok
        and not ttp_would
        and ttp_room <= 1.5  # within 1.5pp of trail width
    )
    dist_to_arm = float(ttp["arm_gain_pct"]) - peak_gain

    # --- ATR trailing stop (approx with min_trail when no live ATR) ---
    raw_trail = atr_pct_est * float(ts["atr_multiplier"])
    ts_trail = max(float(ts["min_trail_pct"]), min(float(ts["max_trail_pct"]), raw_trail))
    ts_active = bool(ts.get("enabled", True)) and gain >= float(ts["activation_gain_pct"])
    ts_room = ts_trail - drop_from_high
    ts_fire_price = recent_high * (1.0 - ts_trail / 100.0) if recent_high else 0.0
    ts_would = ts_active and drop_from_high >= ts_trail
    ts_near = ts_active and not ts_would and ts_room <= 2.0

    # --- Stop loss ---
    sl_price = entry * (1.0 - sl_pct / 100.0)
    partial_sl_price = entry * (1.0 - partial_sl / 100.0)
    sl_dist = gain + sl_pct  # how many pp above hard SL (0 = at SL)
    sl_would = gain <= -sl_pct
    partial_sl_would = gain <= -partial_sl
    sl_near = not sl_would and sl_dist <= 8.0  # within 8pp of SL

    # --- TP tiers ---
    tiers = [float(t) for t in (pos.get("take_profit_tiers") or [])]
    tiers_hit = [t for t in tiers if gain >= t]
    next_tier = next((t for t in tiers if gain < t), None)
    dist_next_tier = (next_tier - gain) if next_tier is not None else None

    # --- Safety TP ---
    safety_pct = pos.get("safety_tp_pct")
    safety_min = pos.get("safety_tp_min_gain_pct")
    safety_would = False
    if safety_pct is not None and safety_min is not None:
        # simplified: peak reached safety_min and price still above safety_tp band
        safety_would = peak_gain >= float(safety_min) and gain >= float(safety_pct)

    # --- Profit max lifetime ---
    hold_h = hours_since(pos.get("first_buy_at"))
    life_arm = float(life.get("arm_gain_pct") or 3)
    life_max_h = float(life.get("max_hours") or 96)
    life_min_g = float(life.get("min_gain_pct") or 1)
    life_skip_peak = float(life.get("skip_if_peak_above_pct") or 999)
    profit_armed = bool(pos.get("profit_armed_at")) or (
        life.get("enabled") and peak_gain >= life_arm
    )
    # arm in viz when peak crosses life arm (don't mutate ledger)
    if life.get("enabled") and peak_gain >= life_arm:
        profit_armed = True
    life_skip = peak_gain >= life_skip_peak
    life_would = False
    life_progress = 0.0
    if life.get("enabled") and profit_armed and not life_skip and hold_h is not None:
        life_progress = min(1.0, hold_h / max(0.01, life_max_h))
        life_would = hold_h >= life_max_h and gain >= life_min_g

    # --- Soft TA gates (thresholds only — no RSI/BB without candles) ---
    rsi_gate = pos.get("rsi_sell_min_gain_pct")
    bb_gate = pos.get("bb_sell_min_gain_pct")
    rsi_gate_met = rsi_gate is not None and gain >= float(rsi_gate)
    bb_gate_met = bb_gate is not None and gain >= float(bb_gate)

    would_sources: list[str] = []
    if ttp_would:
        would_sources.append("trailing_take_profit")
    if ts_would:
        would_sources.append("trailing_stop")
    if sl_would:
        would_sources.append("stop_loss")
    elif partial_sl_would:
        would_sources.append("partial_stop")
    if life_would:
        would_sources.append("profit_max_lifetime")
    if safety_would:
        would_sources.append("safety_tp")

    near_sources: list[str] = []
    if ttp_near:
        near_sources.append("trailing_take_profit")
    if ts_near:
        near_sources.append("trailing_stop")
    if sl_near:
        near_sources.append("stop_loss")
    if (
        life.get("enabled")
        and profit_armed
        and not life_skip
        and hold_h is not None
        and life_progress >= 0.85
        and not life_would
    ):
        near_sources.append("profit_max_lifetime")
    if next_tier is not None and dist_next_tier is not None and dist_next_tier <= 3.0:
        near_sources.append(f"tp_tier_{int(next_tier)}")

    # recovery_hold / sniper_focus: mirror bot sell gates (Hard SL still fires)
    recovery_hold = recovery_hold_flag
    blocked_by_hold: list[str] = []
    raw_would = list(would_sources)
    raw_near = list(near_sources)
    if recovery_hold:
        try:
            from strategies.position_gates import filter_would_sources_for_hold

            would_sources, blocked_by_hold = filter_would_sources_for_hold(
                would_sources, recovery_hold=True
            )
            near_sources, near_blocked = filter_would_sources_for_hold(
                near_sources, recovery_hold=True
            )
            blocked_by_hold = list(dict.fromkeys(blocked_by_hold + near_blocked))
        except Exception:
            blocked_by_hold = [
                s
                for s in would_sources
                if s
                in {
                    "trailing_take_profit",
                    "trailing_stop",
                    "partial_stop",
                    "profit_max_lifetime",
                    "safety_tp",
                }
                or str(s).startswith("tp_tier_")
            ]
            would_sources = [s for s in would_sources if s not in blocked_by_hold]
        if ttp_would:
            ttp_would = False
        if ts_would:
            ts_would = False
        if life_would and "profit_max_lifetime" in blocked_by_hold:
            life_would = False

    # BE+ distance for hold promote (avg × 1.02)
    be_buffer = 2.0
    try:
        from strategies.recovery_hold import recovery_hold_config

        be_buffer = float(recovery_hold_config().get("be_buffer_pct") or 2.0)
    except Exception:
        pass
    be_price = entry * (1.0 + be_buffer / 100.0)
    be_dist_pp = ((price / be_price) - 1.0) * 100.0 if be_price > 0 else None
    be_plus_ready = recovery_hold and price >= be_price

    # urgency: higher = more interesting on the radar
    urgency = 0.0
    if would_sources:
        urgency = 100.0 + len(would_sources) * 10
    elif blocked_by_hold:
        urgency = 55.0  # hold blocking interesting trail
    elif near_sources:
        urgency = 60.0 + (10.0 - min(ttp_room if ttp_near else 10, 10))
    else:
        if ttp_armed:
            urgency = 40.0 + max(0.0, 10.0 - ttp_room)
        elif peak_gain > 0:
            arm = float(ttp["arm_gain_pct"])
            urgency = 20.0 * max(0.0, min(1.0, peak_gain / max(arm, 1.0)))
        if gain < 0:
            urgency = max(urgency, 15.0 * min(1.0, abs(gain) / max(sl_pct, 1.0)))
        if recovery_hold:
            urgency = max(urgency, 35.0)

    notional = float(pos.get("amount") or 0) * price
    try:
        from strategies.short_math import snapshot as _short_snap

        _ss = _short_snap(pos, price)
        pnl_usdt = float(_ss.get("pnl") or 0)
    except Exception:
        pnl_usdt = float(pos.get("amount") or 0) * (price - entry)

    status = "idle"
    if would_sources:
        status = "would_exit"
    elif recovery_hold and blocked_by_hold:
        status = "recovery_hold"
    elif recovery_hold:
        status = "recovery_hold"
    elif near_sources:
        status = "near_exit"
    elif ttp_armed or ts_active:
        status = "armed"
    elif gain > 0:
        status = "in_profit"
    elif gain < -1:
        status = "in_loss"

    return {
        "ok": True,
        "symbol": pos["symbol"],
        "timeframe": pos.get("timeframe"),
        "price": price,
        "entry": entry,
        "recent_high": recent_high,
        "peak_epoch_high": epoch or None,
        "gain_pct": round(gain, 3),
        "peak_gain_pct": round(peak_gain, 3),
        "drop_from_high_pct": round(drop_from_high, 3),
        "notional_usdt": round(notional, 2),
        "pnl_usdt": round(pnl_usdt, 2),
        "amount": pos.get("amount"),
        "sold_percent": pos.get("sold_percent"),
        "dca_rounds": pos.get("dca_rounds"),
        "strategy_tier": pos.get("strategy_tier"),
        "recovery_hold": recovery_hold,
        "sniper_focus": bool(pos.get("sniper_focus")),
        "dca_heavy_used": bool(pos.get("dca_heavy_used")),
        "position_locked": bool(pos.get("position_locked")),
        "lock_modes": list(pos.get("lock_modes") or []),
        "be_plus": {
            "buffer_pct": be_buffer,
            "price": be_price,
            "dist_pp": round(be_dist_pp, 2) if be_dist_pp is not None else None,
            "ready": be_plus_ready,
        },
        "status": status,
        "urgency": round(urgency, 2),
        "would_exit": bool(would_sources),
        "would_sources": would_sources,
        "near_exit": bool(near_sources),
        "near_sources": near_sources,
        "blocked_by_hold": blocked_by_hold,
        "raw_would_sources": raw_would,
        "raw_near_sources": raw_near,
        "prefer_full_close": bool(pos.get("prefer_full_close", True)),
        "ttp": {
            "enabled": bool(ttp.get("enabled")),
            "armed": ttp_armed,
            "arm_gain_pct": float(ttp["arm_gain_pct"]),
            "dist_to_arm_pp": round(dist_to_arm, 2),
            "trail_pct": round(ttp_trail, 2),
            "drop_pct": round(drop_from_high, 3),
            "room_pp": round(ttp_room, 3),
            "fire_price": ttp_fire_price,
            "min_gain_pct": float(ttp["min_gain_pct"]),
            "would": ttp_would and not recovery_hold,
            "near": ttp_near and not recovery_hold,
            "blocked_hold": recovery_hold and "trailing_take_profit" in blocked_by_hold,
        },
        "trailing_stop": {
            "enabled": bool(ts.get("enabled", True)),
            "active": ts_active,
            "activation_gain_pct": float(ts["activation_gain_pct"]),
            "trail_pct": round(ts_trail, 2),
            "room_pp": round(ts_room, 3),
            "fire_price": ts_fire_price,
            "would": ts_would and not recovery_hold,
            "near": ts_near and not recovery_hold,
            "blocked_hold": recovery_hold and "trailing_stop" in blocked_by_hold,
            "atr_pct_est": atr_pct_est,
        },
        "stop_loss": {
            "pct": sl_pct,
            "price": sl_price,
            "dist_pp": round(sl_dist, 2),
            "would": sl_would,
            "near": sl_near,
            "partial_pct": partial_sl,
            "partial_price": partial_sl_price,
            "partial_would": partial_sl_would,
        },
        "tp_tiers": {
            "tiers": tiers,
            "hit": tiers_hit,
            "next": next_tier,
            "dist_next_pp": round(dist_next_tier, 2) if dist_next_tier is not None else None,
        },
        "safety_tp": {
            "pct": safety_pct,
            "min_gain_pct": safety_min,
            "would": safety_would,
        },
        "life": {
            "enabled": bool(life.get("enabled")),
            "armed": profit_armed,
            "hold_hours": round(hold_h, 1) if hold_h is not None else None,
            "max_hours": life_max_h,
            "progress": round(life_progress, 3),
            "skip_runner": life_skip,
            "min_gain_pct": life_min_g,
            "would": life_would,
        },
        "ta_gates": {
            "rsi_min_gain_pct": rsi_gate,
            "rsi_gate_met": rsi_gate_met,
            "bb_min_gain_pct": bb_gate,
            "bb_gate_met": bb_gate_met,
            "note": "RSI/BB values need candles — only gain gates shown",
        },
    }



