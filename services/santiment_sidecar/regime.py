"""Multi-score regime mapping from Santiment features + meta (no I/O).

Scores (each -1 risk-off … +1 risk-on when present):
  onchain  — DAA (+ soft dev bias)
  leverage — funding/OI when live (P3); None until then
  social   — only if meta.social_fresh

vol_penalty 0..1 reduces composite. Composite → CRASH / RISK_OFF / NEUTRAL / RISK_ON.
RISK_ON size ≤ 0.9. Lagged social alone never drives policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
    scores: dict[str, Any] = field(default_factory=dict)


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
    b = _f(features, btc_key)
    e = _f(features, eth_key)
    if b is None and e is None:
        return None
    if b is None:
        return e
    if e is None:
        return b
    return max(b, e)


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def score_onchain(daa_d: float | None, dev_d: float | None) -> float | None:
    if daa_d is None and dev_d is None:
        return None
    # DAA dominates; scale so ±0.4 Δ ≈ ±1 score.
    base = _clamp((daa_d if daa_d is not None else 0.0) * 2.5)
    if dev_d is not None and daa_d is not None and daa_d < 0 and dev_d <= -0.3:
        base = _clamp(base - 0.15)
    elif dev_d is not None and daa_d is None:
        base = _clamp(dev_d * 1.5)
    return round(base, 4)


def score_social(social_d: float | None) -> float | None:
    if social_d is None:
        return None
    return round(_clamp(social_d * 2.0), 4)


def score_leverage(features: dict[str, float], meta: dict[str, Any]) -> float | None:
    """Funding (+ optional OI Δ) only when meta.leverage_fresh (live SanAPI)."""
    if not meta.get("leverage_fresh"):
        return None
    fr = _f(features, "btc_funding_rate")
    if fr is None:
        fr = _f(features, "btc_funding")
    if fr is None:
        return None
    # Typical funding ~1e-4 to 1e-3; extreme positive → crowded long → negative score.
    # Map: 0 → 0, +0.001 → -0.5, -0.001 → +0.5
    score = _clamp(-float(fr) * 500.0)
    oi_d = _f(features, "btc_open_interest_delta_1d")
    if oi_d is not None and oi_d <= -0.08:
        # OI dumping → deleveraging stress (slightly more risk-off for new longs)
        score = _clamp(score - 0.15)
    elif oi_d is not None and oi_d >= 0.1 and fr is not None and fr > 0:
        # Rising OI + positive funding → crowded long
        score = _clamp(score - 0.1)
    return round(score, 4)


def vol_penalty(vol: float | None) -> float:
    if vol is None:
        return 0.0
    if vol >= 0.08:
        return 0.55
    if vol >= 0.04:
        return 0.3
    if vol >= 0.025:
        return 0.12
    return 0.0


def composite_score(
    onchain: float | None,
    leverage: float | None,
    social: float | None,
    *,
    vol_pen: float,
) -> tuple[float | None, list[str]]:
    """Weighted mean of available pillar scores, then subtract vol penalty.

    Returns (composite in approx -1.5..1.0, list of pillars used).
    """
    parts: list[tuple[str, float, float]] = []  # name, value, weight
    if onchain is not None:
        parts.append(("onchain", onchain, 0.55))
    if leverage is not None:
        parts.append(("leverage", leverage, 0.25))
    if social is not None:
        parts.append(("social", social, 0.2))
    if not parts:
        return None, []
    wsum = sum(w for _, _, w in parts)
    raw = sum(v * w for _, v, w in parts) / wsum
    return round(raw - vol_pen, 4), [n for n, _, _ in parts]


def confidence_from_coverage(
    pillars: list[str],
    *,
    has_vol: bool,
    social_fresh: bool,
) -> float:
    n = len(pillars)
    conf = 0.35 + 0.15 * n
    if has_vol:
        conf += 0.1
    if social_fresh and "social" in pillars:
        conf += 0.05
    if "leverage" in pillars:
        conf += 0.05
    return round(min(0.92, conf), 4)


def decide_regime(
    features: dict[str, float] | None,
    meta: dict[str, Any] | None = None,
) -> RegimeDecision:
    feat = dict(features or {})
    meta = dict(meta or {})

    empty_scores = {
        "onchain": None,
        "leverage": None,
        "social": None,
        "vol_penalty": 0.0,
        "composite": None,
        "pillars": [],
    }

    if not feat:
        return RegimeDecision(
            regime="NEUTRAL",
            confidence=0.3,
            size_mult=1.0,
            sensor_policy="active",
            max_new_entries_per_hour=20,
            rationale="no Santiment features — neutral fail-open",
            scores=empty_scores,
        )

    social_fresh = bool(meta.get("social_fresh"))
    daa_d = _blend_delta(feat, "btc_daa_delta_1d", "eth_daa_delta_1d")
    vol = _blend_level(feat, "btc_vol_1d", "eth_vol_1d")
    dev_d = _blend_delta(feat, "btc_dev_activity_delta_1d", "eth_dev_activity_delta_1d")
    social_d = None
    if social_fresh:
        social_d = _blend_delta(
            feat, "btc_social_volume_delta_1d", "eth_social_volume_delta_1d"
        )

    onchain = score_onchain(daa_d, dev_d)
    leverage = score_leverage(feat, meta)
    social = score_social(social_d) if social_fresh else None
    vpen = vol_penalty(vol)
    composite, pillars = composite_score(onchain, leverage, social, vol_pen=vpen)

    scores = {
        "onchain": onchain,
        "leverage": leverage,
        "social": social,
        "vol_penalty": round(vpen, 4),
        "composite": composite,
        "pillars": pillars,
        "daa_d": round(daa_d, 4) if daa_d is not None else None,
        "vol": round(vol, 6) if vol is not None else None,
        "dev_d": round(dev_d, 4) if dev_d is not None else None,
        "social_d": round(social_d, 4) if social_d is not None else None,
    }

    if composite is None:
        return RegimeDecision(
            regime="NEUTRAL",
            confidence=0.35,
            size_mult=1.0,
            sensor_policy="active",
            max_new_entries_per_hour=20,
            rationale=(
                "no policy-fresh onchain/leverage/social — neutral fail-open "
                f"(keys={len(feat)})"
            ),
            scores=scores,
        )

    conf = confidence_from_coverage(
        pillars, has_vol=vol is not None, social_fresh=social_fresh
    )
    why = (
        f"comp={composite:+.2f} onchain={onchain} lev={leverage} "
        f"social={social} vpen={vpen:.2f}"
    )

    # CRASH: deep negative composite + DAA dump + material vol
    if (
        composite <= -0.7
        and daa_d is not None
        and daa_d <= -0.25
        and vol is not None
        and vol >= 0.05
    ):
        return RegimeDecision(
            regime="CRASH",
            confidence=min(0.92, conf + 0.1),
            size_mult=0.0,
            sensor_policy="block",
            max_new_entries_per_hour=0,
            rationale=f"extreme multi-score stress ({why})",
            scores=scores,
        )

    if composite <= -0.35 or (daa_d is not None and daa_d <= -0.3):
        size = 0.35 if composite <= -0.55 else 0.5
        return RegimeDecision(
            regime="RISK_OFF",
            confidence=conf,
            size_mult=size,
            sensor_policy="shadow",
            max_new_entries_per_hour=2,
            rationale=f"risk-off multi-score ({why})",
            scores=scores,
        )

    if composite >= 0.3 and (vol is None or vol < 0.04):
        return RegimeDecision(
            regime="RISK_ON",
            confidence=conf,
            size_mult=RISK_ON_SIZE_CAP,
            sensor_policy="active",
            max_new_entries_per_hour=25,
            rationale=f"soft risk-on size_cap={RISK_ON_SIZE_CAP} ({why})",
            scores=scores,
        )

    size = 0.85
    if vol is not None and vol >= 0.035:
        size = 0.75
    return RegimeDecision(
        regime="NEUTRAL",
        confidence=conf,
        size_mult=size,
        sensor_policy="active",
        max_new_entries_per_hour=15,
        rationale=f"neutral multi-score ({why})",
        scores=scores,
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
