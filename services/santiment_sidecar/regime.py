"""Pure regime mapping from Santiment-style features (no I/O)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RegimeDecision:
    regime: str
    confidence: float
    size_mult: float
    sensor_policy: str  # active | shadow | block
    max_new_entries_per_hour: int
    rationale: str


def _zish_delta(features: dict, key: str) -> float:
    return float(features.get(f"{key}_delta_1d") or 0.0)


def decide_regime(features: dict[str, float] | None) -> RegimeDecision:
    """Map sparse features → coarse market policy for the trading bot.

    Calibrate on paper; defaults are intentionally conservative when social
    volume is falling hard and softer when rising.
    """
    feat = dict(features or {})
    if not feat:
        return RegimeDecision(
            regime="NEUTRAL",
            confidence=0.3,
            size_mult=1.0,
            sensor_policy="active",
            max_new_entries_per_hour=20,
            rationale="no Santiment features — neutral fail-open",
        )

    btc_sv_d = _zish_delta(feat, "btc_social_volume")
    eth_sv_d = _zish_delta(feat, "eth_social_volume")
    btc_dev_d = _zish_delta(feat, "btc_dev_activity")
    eth_dev_d = _zish_delta(feat, "eth_dev_activity")
    # Prefer social volume; fall back to dev activity when social is plan-lagged.
    if any(k.endswith("_social_volume_delta_1d") for k in feat):
        social_d = 0.6 * btc_sv_d + 0.4 * eth_sv_d
    else:
        social_d = 0.5 * btc_dev_d + 0.5 * eth_dev_d

    # Strong social collapse → risk-off/crash; expansion → risk-on soft.
    if social_d <= -0.55:
        return RegimeDecision(
            regime="CRASH",
            confidence=0.75,
            size_mult=0.0,
            sensor_policy="block",
            max_new_entries_per_hour=0,
            rationale=f"severe social collapse social_d={social_d:+.2f}",
        )
    if social_d <= -0.35:
        return RegimeDecision(
            regime="RISK_OFF",
            confidence=min(0.9, 0.5 + abs(social_d)),
            size_mult=0.35,
            sensor_policy="shadow",
            max_new_entries_per_hour=2,
            rationale=(
                f"social volume down (btc_d={btc_sv_d:+.2f}, eth_d={eth_sv_d:+.2f})"
            ),
        )
    if social_d >= 0.4 and btc_dev_d >= -0.2:
        return RegimeDecision(
            regime="RISK_ON",
            confidence=min(0.85, 0.45 + social_d * 0.5),
            size_mult=1.0,
            sensor_policy="active",
            max_new_entries_per_hour=30,
            rationale=(
                f"social volume expanding (btc_d={btc_sv_d:+.2f}, eth_d={eth_sv_d:+.2f})"
            ),
        )

    return RegimeDecision(
        regime="NEUTRAL",
        confidence=0.55,
        size_mult=0.85,
        sensor_policy="active",
        max_new_entries_per_hour=15,
        rationale=f"mixed social (social_d={social_d:+.2f})",
    )


def should_push(
    prev: dict | None,
    new: dict,
    *,
    size_delta: float = 0.1,
    heartbeat_due: bool = False,
) -> bool:
    if heartbeat_due:
        return True
    if not prev:
        return True
    if prev.get("regime") != new.get("regime"):
        return True
    if prev.get("sensor_policy") != new.get("sensor_policy"):
        return True
    try:
        old_m = float(prev.get("size_mult") or 1)
        new_m = float(new.get("size_mult") or 1)
        if abs(new_m - old_m) >= size_delta:
            return True
    except Exception:
        return True
    return False
