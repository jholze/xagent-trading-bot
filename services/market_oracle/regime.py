"""Pure market-state machine from BTC/ETH features + hysteresis.

A1: multi-TF returns, EMA structure, 1h cascade → CRASH (same hysteresis as other states).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class OracleDecision:
    state: str
    confidence: float
    size_mult: float
    sensor_policy: str
    block_new_entries: bool
    block_sensor_entries: bool
    max_new_buys_per_hour: int
    rationale: str
    bars_in_state: int = 1
    measured: bool = True


def raw_state_from_features(
    features: dict[str, float],
    *,
    risk_off_24h: float = -3.0,
    crash_24h: float = -6.0,
    risk_on_24h: float = 1.0,
    cascade_1h: float = -2.5,
    risk_on_1h_floor: float = -1.0,
    breadth_risk_on_min_green: float = 0.45,
    breadth_risk_off_max_green: float = 0.35,
    breadth_rotten_max_green: float = 0.25,
    funding_extreme_pos: float = 0.05,
    funding_extreme_neg: float = -0.03,
    funding_crash_1h: float = -1.5,
    funding_crash_24h_blend: float = -2.0,
) -> tuple[str, float, str]:
    """Map features → provisional state (no hysteresis).

    Breadth/funding optional: if missing, price rules only and measured=False.
    Funding unit: percent per interval (0.01 = 0.01%).
    """
    btc = float(features.get("btc_ret_24h_pct") or 0.0)
    eth = float(features.get("eth_ret_24h_pct") or 0.0)
    blend = 0.7 * btc + 0.3 * eth
    trend = float(features.get("btc_trend_4h") or 0.0)
    btc_1h = features.get("btc_ret_1h_pct")
    has_1h = btc_1h is not None
    btc_1h_f = float(btc_1h) if has_1h else 0.0
    btc_4h = features.get("btc_ret_4h_pct")
    btc_7d = features.get("btc_ret_7d_pct")
    has_breadth = features.get("breadth_pct_green") is not None
    pct_green = float(features["breadth_pct_green"]) if has_breadth else None
    med_br = features.get("breadth_median_24h_pct")
    has_funding = features.get("btc_funding_rate_pct") is not None
    funding = float(features["btc_funding_rate_pct"]) if has_funding else None

    parts = [f"btc_24h={btc:+.2f}%", f"eth_24h={eth:+.2f}%"]
    if has_1h:
        parts.append(f"btc_1h={btc_1h_f:+.2f}%")
    if btc_4h is not None:
        parts.append(f"btc_4h={float(btc_4h):+.2f}%")
    if btc_7d is not None:
        parts.append(f"btc_7d={float(btc_7d):+.2f}%")
    parts.append(f"trend_4h={trend:+.0f}")
    if has_breadth and pct_green is not None:
        parts.append(f"breadth_green={pct_green:.0%}")
        if med_br is not None:
            parts.append(f"breadth_med={float(med_br):+.2f}%")
    if has_funding and funding is not None:
        parts.append(f"fund={funding:+.4f}%")
    base = " ".join(parts)

    # A1 cascade: sharp 1h dump → CRASH without waiting for −6% 24h
    if has_1h and btc_1h_f <= cascade_1h:
        conf = min(0.95, 0.65 + abs(btc_1h_f) / 15.0)
        return "CRASH", conf, f"{base} cascade_1h"

    if blend <= crash_24h or btc <= crash_24h:
        conf = min(0.95, 0.6 + abs(blend) / 20.0)
        return "CRASH", conf, f"{base} crash_24h"

    # A3: crowded long funding + price already dumping → CRASH
    if (
        has_funding
        and funding is not None
        and funding >= funding_extreme_pos
        and (
            blend <= funding_crash_24h_blend
            or (has_1h and btc_1h_f <= funding_crash_1h)
        )
    ):
        conf = min(0.92, 0.6 + abs(funding) * 2.0)
        return "CRASH", conf, f"{base} funding_crash"

    if blend <= risk_off_24h or btc <= risk_off_24h:
        conf = min(0.9, 0.55 + abs(blend) / 15.0)
        return "RISK_OFF", conf, f"{base} risk_off"

    # A3: extreme positive funding + soft red/flat → RISK_OFF
    if has_funding and funding is not None and funding >= funding_extreme_pos and blend < 0.5:
        conf = 0.68
        return "RISK_OFF", conf, f"{base} funding_crowded_long"

    # Soft: 4h dump + weak trend → RISK_OFF even if 24h mild
    if btc_4h is not None and float(btc_4h) <= -2.0 and trend < 0:
        conf = 0.62
        return "RISK_OFF", conf, f"{base} structure_4h_down"

    # A2 breadth: rotten book → RISK_OFF (even if BTC only mildly red/flat)
    if has_breadth and pct_green is not None:
        if pct_green <= breadth_rotten_max_green:
            conf = 0.7
            return "RISK_OFF", conf, f"{base} breadth_rotten"
        if pct_green <= breadth_risk_off_max_green and blend < 0.5:
            conf = 0.65
            return "RISK_OFF", conf, f"{base} breadth_weak"

    # RISK_ON: 24h green + trend up + 1h not strongly negative + breadth ok
    def _risk_on_ok() -> tuple[bool, str]:
        if has_1h and btc_1h_f <= risk_on_1h_floor:
            return False, "risk_on_blocked_1h"
        if has_breadth and pct_green is not None and pct_green < breadth_risk_on_min_green:
            return False, "risk_on_blocked_breadth"
        return True, ""

    if blend >= risk_on_24h and trend > 0:
        ok, block = _risk_on_ok()
        if not ok:
            conf = 0.55
            return "NEUTRAL", conf, f"{base} {block}"
        conf = min(0.88, 0.5 + blend / 15.0)
        return "RISK_ON", conf, f"{base} risk_on"

    # A3 bullish soft: extreme negative funding (shorts crowded) + mild green/flat + trend
    if (
        has_funding
        and funding is not None
        and funding <= funding_extreme_neg
        and blend >= 0.0
        and trend > 0
    ):
        ok, block = _risk_on_ok()
        if ok:
            conf = 0.62
            return "RISK_ON", conf, f"{base} funding_short_crowded_risk_on"
        conf = 0.55
        return "NEUTRAL", conf, f"{base} funding_short_crowded_{block or 'blocked'}"

    conf = 0.55
    return "NEUTRAL", conf, f"{base} neutral"


def policy_for_state(state: str, *, risk_off_size: float = 0.35, neutral_size: float = 0.85) -> dict:
    s = (state or "NEUTRAL").upper()
    if s == "CRASH":
        return {
            "size_mult": 0.0,
            "sensor_policy": "block",
            "block_new_entries": True,
            "block_sensor_entries": True,
            "max_new_buys_per_hour": 0,
        }
    if s == "RISK_OFF":
        return {
            "size_mult": float(risk_off_size),
            "sensor_policy": "shadow",
            "block_new_entries": False,
            "block_sensor_entries": True,
            "max_new_buys_per_hour": 2,
        }
    if s == "RISK_ON":
        return {
            "size_mult": 1.0,
            "sensor_policy": "active",
            "block_new_entries": False,
            "block_sensor_entries": False,
            "max_new_buys_per_hour": 30,
        }
    return {
        "size_mult": float(neutral_size),
        "sensor_policy": "active",
        "block_new_entries": False,
        "block_sensor_entries": False,
        "max_new_buys_per_hour": 15,
    }


class StateHysteresis:
    """Require min consecutive raw states before flipping."""

    def __init__(self, min_bars_to_flip: int = 2):
        self.min_bars = max(1, int(min_bars_to_flip))
        self.state = "NEUTRAL"
        self.pending: str | None = None
        self.pending_count = 0
        self.bars_in_state = 0

    def update(self, raw_state: str) -> tuple[str, int]:
        raw = (raw_state or "NEUTRAL").upper()
        if raw == self.state:
            self.pending = None
            self.pending_count = 0
            self.bars_in_state += 1
            return self.state, self.bars_in_state
        if raw == self.pending:
            self.pending_count += 1
        else:
            self.pending = raw
            self.pending_count = 1
        if self.pending_count >= self.min_bars:
            self.state = raw
            self.pending = None
            self.pending_count = 0
            self.bars_in_state = 1
        else:
            self.bars_in_state += 1
        return self.state, self.bars_in_state


def decide(
    features: dict[str, float],
    hyst: StateHysteresis,
    *,
    risk_off_24h: float = -3.0,
    crash_24h: float = -6.0,
    risk_on_24h: float = 1.0,
    cascade_1h: float = -2.5,
    risk_on_1h_floor: float = -1.0,
    breadth_risk_on_min_green: float = 0.45,
    breadth_risk_off_max_green: float = 0.35,
    breadth_rotten_max_green: float = 0.25,
    funding_extreme_pos: float = 0.05,
    funding_extreme_neg: float = -0.03,
    funding_crash_1h: float = -1.5,
    funding_crash_24h_blend: float = -2.0,
    risk_off_size: float = 0.35,
    neutral_size: float = 0.85,
) -> OracleDecision:
    raw, conf, why = raw_state_from_features(
        features,
        risk_off_24h=risk_off_24h,
        crash_24h=crash_24h,
        risk_on_24h=risk_on_24h,
        cascade_1h=cascade_1h,
        risk_on_1h_floor=risk_on_1h_floor,
        breadth_risk_on_min_green=breadth_risk_on_min_green,
        breadth_risk_off_max_green=breadth_risk_off_max_green,
        breadth_rotten_max_green=breadth_rotten_max_green,
        funding_extreme_pos=funding_extreme_pos,
        funding_extreme_neg=funding_extreme_neg,
        funding_crash_1h=funding_crash_1h,
        funding_crash_24h_blend=funding_crash_24h_blend,
    )
    state, bars = hyst.update(raw)
    pol = policy_for_state(state, risk_off_size=risk_off_size, neutral_size=neutral_size)
    if state != raw:
        why = f"{why} | holding {state} (raw={raw}, flip {hyst.pending_count}/{hyst.min_bars})"
    has_price = features.get("btc_ret_24h_pct") is not None
    has_breadth = features.get("breadth_pct_green") is not None
    has_funding = features.get("btc_funding_rate_pct") is not None
    measured = bool(has_price and has_breadth and has_funding)
    return OracleDecision(
        state=state,
        confidence=conf,
        size_mult=float(pol["size_mult"]),
        sensor_policy=str(pol["sensor_policy"]),
        block_new_entries=bool(pol["block_new_entries"]),
        block_sensor_entries=bool(pol["block_sensor_entries"]),
        max_new_buys_per_hour=int(pol["max_new_buys_per_hour"]),
        rationale=why,
        bars_in_state=bars,
        measured=measured,
    )


def should_push(
    prev: dict | None,
    new: dict,
    *,
    size_delta: float = 0.1,
    heartbeat_due: bool = False,
) -> bool:
    if heartbeat_due or not prev:
        return True
    prev_state = prev.get("state") or prev.get("regime")
    new_state = new.get("state") or new.get("regime")
    if prev_state != new_state:
        return True
    if prev.get("sensor_policy") != new.get("sensor_policy"):
        return True
    try:
        if abs(float(prev.get("size_mult") or 1) - float(new.get("size_mult") or 1)) >= size_delta:
            return True
    except Exception:
        return True
    return False
