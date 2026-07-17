"""Pure regime mapping from Santiment features + meta (no I/O).

P0: primary = DAA + volatility; social only if meta.social_fresh;
dev soft bias; CRASH allowed on extreme live stress; RISK_ON size ≤ 0.9.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


RISK_ON_SIZE_CAP = 0.9


@dataclass(frozen=True)
class RegimeDecision:
    regime: str
    confidence: float
    size_mult: float
    sensor_policy: str  # active | shadow | block
    max_new_entries_per_hour: int
    rationale: str


def _f(features: dict, key: str, default: float | None = None) -> float | None:
    if key not in features or features.get(key) is None:
        return default
    try:
        return float(features[key])
    except Exception:
        return default


def _blend_delta(features: dict, btc_key: str, eth_key: str) -> float | None:
    b = _f(features, btc_key)
    e = _f(features, eth_key)
    if b is None and e is None:
        return None
    if b is None:
        return e
    if e is None:
        return b
    return 0.6 * b + 0.4 * e


def _blend_level(features: dict, btc_key: str, eth_key: str) -> float | None:
    """Prefer max of available levels (stress uses worst vol)."""
    b = _f(features, btc_key)
    e = _f(features, eth_key)
    if b is None and e is None:
        return None
    if b is None:
        return e
    if e is None:
        return b
    return max(b, e)


def decide_regime(
    features: dict[str, float] | None,
    meta: dict[str, Any] | None = None,
) -> RegimeDecision:
    """Map features → coarse market policy for the trading bot.

    Social volume only influences policy when meta.social_fresh is True
    (realtime-successful fetch). Lagged social alone must not drive CRASH/RISK_OFF.
    """
    feat = dict(features or {})
    meta = dict(meta or {})

    if not feat:
        return RegimeDecision(
            regime="NEUTRAL",
            confidence=0.3,
            size_mult=1.0,
            sensor_policy="active",
            max_new_entries_per_hour=20,
            rationale="no Santiment features — neutral fail-open",
        )

    social_fresh = bool(meta.get("social_fresh"))
    policy_inputs = list(meta.get("policy_inputs") or [])

    daa_d = _blend_delta(feat, "btc_daa_delta_1d", "eth_daa_delta_1d")
    vol = _blend_level(feat, "btc_vol_1d", "eth_vol_1d")
    dev_d = _blend_delta(feat, "btc_dev_activity_delta_1d", "eth_dev_activity_delta_1d")
    social_d = None
    if social_fresh:
        social_d = _blend_delta(
            feat, "btc_social_volume_delta_1d", "eth_social_volume_delta_1d"
        )
        if social_d is not None and "social" not in policy_inputs:
            policy_inputs.append("social")

    # No live policy anchors → fail-open (ignore orphan social-only lag keys).
    has_daa = daa_d is not None
    has_vol = vol is not None
    if not has_daa and not has_vol and not (social_fresh and social_d is not None):
        return RegimeDecision(
            regime="NEUTRAL",
            confidence=0.35,
            size_mult=1.0,
            sensor_policy="active",
            max_new_entries_per_hour=20,
            rationale=(
                "no policy-fresh DAA/vol/social — neutral fail-open "
                f"(keys={len(feat)})"
            ),
        )

    stress = 0.0
    parts: list[str] = []

    if has_daa:
        # daa falling → stress
        if daa_d <= -0.25:
            stress += min(0.55, 0.25 + abs(daa_d) * 0.5)
        elif daa_d <= -0.12:
            stress += 0.2
        elif daa_d >= 0.15:
            stress -= 0.15
        parts.append(f"daa_d={daa_d:+.2f}")

    if has_vol:
        if vol >= 0.08:
            stress += 0.4
            parts.append(f"vol={vol:.3f} extreme")
        elif vol >= 0.04:
            stress += 0.22
            parts.append(f"vol={vol:.3f} high")
        elif vol >= 0.025:
            stress += 0.08
            parts.append(f"vol={vol:.3f}")
        else:
            parts.append(f"vol={vol:.3f}")

    # Soft social (only when fresh)
    if social_d is not None:
        if social_d <= -0.35:
            stress += 0.18
        elif social_d <= -0.2:
            stress += 0.1
        elif social_d >= 0.35:
            stress -= 0.08
        parts.append(f"social_d={social_d:+.2f}")

    # Soft dev bias
    if dev_d is not None:
        if dev_d <= -0.3 and (daa_d is not None and daa_d < 0):
            stress += 0.1
            parts.append(f"dev_d={dev_d:+.2f} soft")
        elif dev_d is not None:
            parts.append(f"dev_d={dev_d:+.2f}")

    stress = max(-0.3, min(1.2, stress))
    why = ", ".join(parts) if parts else "mixed"

    # CRASH: extreme live stress (DAA dump + high vol), optional social/dev soft
    if (
        has_daa
        and has_vol
        and daa_d is not None
        and vol is not None
        and daa_d <= -0.25
        and vol >= 0.05
        and stress >= 0.75
    ):
        return RegimeDecision(
            regime="CRASH",
            confidence=min(0.9, 0.55 + stress * 0.3),
            size_mult=0.0,
            sensor_policy="block",
            max_new_entries_per_hour=0,
            rationale=f"extreme live stress ({why})",
        )

    if stress >= 0.45 or (has_daa and daa_d is not None and daa_d <= -0.3):
        return RegimeDecision(
            regime="RISK_OFF",
            confidence=min(0.9, 0.5 + abs(stress) * 0.35),
            size_mult=0.35 if stress >= 0.6 else 0.5,
            sensor_policy="shadow",
            max_new_entries_per_hour=2,
            rationale=f"risk-off stress={stress:.2f} ({why})",
        )

    # Soft RISK_ON: DAA expanding, vol not high, stress low
    if (
        has_daa
        and daa_d is not None
        and daa_d >= 0.12
        and (vol is None or vol < 0.04)
        and stress <= 0.15
    ):
        return RegimeDecision(
            regime="RISK_ON",
            confidence=min(0.85, 0.5 + daa_d * 0.4),
            size_mult=RISK_ON_SIZE_CAP,
            sensor_policy="active",
            max_new_entries_per_hour=25,
            rationale=f"soft risk-on size_cap={RISK_ON_SIZE_CAP} ({why})",
        )

    size = 0.85
    if has_vol and vol is not None and vol >= 0.035:
        size = 0.75
    return RegimeDecision(
        regime="NEUTRAL",
        confidence=0.55,
        size_mult=size,
        sensor_policy="active",
        max_new_entries_per_hour=15,
        rationale=f"neutral stress={stress:.2f} ({why})",
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
