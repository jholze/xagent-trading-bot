"""Pure market-state machine from BTC/ETH features + hysteresis."""

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


def raw_state_from_features(
    features: dict[str, float],
    *,
    risk_off_24h: float = -3.0,
    crash_24h: float = -6.0,
    risk_on_24h: float = 1.0,
) -> tuple[str, float, str]:
    """Map features → provisional state (no hysteresis)."""
    btc = float(features.get("btc_ret_24h_pct") or 0.0)
    eth = float(features.get("eth_ret_24h_pct") or 0.0)
    blend = 0.7 * btc + 0.3 * eth
    trend = float(features.get("btc_trend_4h") or 0.0)

    if blend <= crash_24h or btc <= crash_24h:
        conf = min(0.95, 0.6 + abs(blend) / 20.0)
        return "CRASH", conf, f"btc_24h={btc:+.2f}% eth_24h={eth:+.2f}% crash"
    if blend <= risk_off_24h or btc <= risk_off_24h:
        conf = min(0.9, 0.55 + abs(blend) / 15.0)
        return "RISK_OFF", conf, f"btc_24h={btc:+.2f}% eth_24h={eth:+.2f}% risk_off"
    if blend >= risk_on_24h and trend >= 0:
        conf = min(0.88, 0.5 + blend / 15.0)
        return "RISK_ON", conf, f"btc_24h={btc:+.2f}% trend_up risk_on"
    conf = 0.55
    return "NEUTRAL", conf, f"btc_24h={btc:+.2f}% eth_24h={eth:+.2f}% neutral"


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
    risk_off_size: float = 0.35,
    neutral_size: float = 0.85,
) -> OracleDecision:
    raw, conf, why = raw_state_from_features(
        features,
        risk_off_24h=risk_off_24h,
        crash_24h=crash_24h,
        risk_on_24h=risk_on_24h,
    )
    state, bars = hyst.update(raw)
    pol = policy_for_state(state, risk_off_size=risk_off_size, neutral_size=neutral_size)
    if state != raw:
        why = f"{why} | holding {state} (raw={raw}, flip {hyst.pending_count}/{hyst.min_bars})"
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
    if prev.get("state") != new.get("state") and prev.get("regime") != new.get("regime"):
        # support both keys
        pass
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
