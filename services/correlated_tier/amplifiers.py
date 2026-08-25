"""Flag-write-side amplifiers for correlated_tier selloff confirmation.

Makes the price-drawdown confirming bar easier when a live market regime or
recent news pulse agrees the tape is risk-off. Amplifiers never replace a
real confirming proxy (effective min_confirming stays >= 1) and never raise.
"""

from __future__ import annotations

from typing import Any

try:
    from services.market_policy_fusion import get_global_market_bias
except Exception:  # pragma: no cover - fail-open if fusion import dies
    def get_global_market_bias(config_raw=None):  # type: ignore[misc]
        return {}

try:
    from intelligence.memory.market_pulse import get_cached_market_pulse
except Exception:  # pragma: no cover - fail-open if pulse import dies
    def get_cached_market_pulse(max_age_sec=None):  # type: ignore[misc]
        return {
            "bearish_score": 0.0,
            "confidence": 0.0,
            "event_count": 0,
            "top_events": [],
        }


def _as_bool(v: Any) -> bool:
    return bool(v)


def apply_amplifiers(payload: dict, group_cfg: dict, ct_cfg: dict) -> dict:
    """Recompute `active` after optional regime/news amplifiers.

    If both amplifier flags are off, return `payload` unchanged (same object).
    On any error the caller should fall back to the original payload; this
    function also fails open internally (no-op amplifiers).
    """
    del group_cfg  # reserved for per-group overrides; unused in v0
    if not isinstance(payload, dict):
        return payload
    cfg = ct_cfg if isinstance(ct_cfg, dict) else {}
    regime_on = _as_bool(cfg.get("regime_amplify_enabled"))
    news_on = _as_bool(cfg.get("news_pulse_enabled"))
    if not regime_on and not news_on:
        return payload

    out = dict(payload)
    regime_amplified = False
    news_amplified = False

    if regime_on:
        try:
            bias = get_global_market_bias() or {}
            regime = str(bias.get("regime") or "").strip().upper()
            allowed = cfg.get("regime_amplify_regimes") or ["RISK_OFF", "CRASH"]
            if not isinstance(allowed, (list, tuple, set)):
                allowed = ["RISK_OFF", "CRASH"]
            allowed_u = {str(x).strip().upper() for x in allowed if x}
            if regime and regime in allowed_u:
                regime_amplified = True
        except Exception:
            regime_amplified = False

    if news_on:
        try:
            pulse = get_cached_market_pulse() or {}
            try:
                thresh = float(cfg.get("news_pulse_bearish_threshold", 0.55) or 0.55)
            except (TypeError, ValueError):
                thresh = 0.55
            try:
                min_conf = float(cfg.get("news_pulse_min_confidence", 0.34) or 0.34)
            except (TypeError, ValueError):
                min_conf = 0.34
            bearish = float(pulse.get("bearish_score") or 0.0)
            conf = float(pulse.get("confidence") or 0.0)
            if bearish >= thresh and conf >= min_conf:
                news_amplified = True
        except Exception:
            news_amplified = False

    raw_delta = int(regime_amplified) + int(news_amplified)
    try:
        cap = int(cfg.get("news_pulse_max_combined_delta", 1) or 0)
    except (TypeError, ValueError):
        cap = 1
    if cap < 0:
        cap = 0
    amplifier_delta = min(raw_delta, cap)

    try:
        min_confirming = int(out.get("min_confirming") or 1)
    except (TypeError, ValueError):
        min_confirming = 1
    try:
        confirming = int(out.get("confirming") or 0)
    except (TypeError, ValueError):
        confirming = 0
    effective_min = max(1, min_confirming - amplifier_delta)
    out["active"] = confirming >= effective_min
    out["amplifier_delta"] = amplifier_delta
    out["regime_amplified"] = bool(regime_amplified)
    out["news_amplified"] = bool(news_amplified)
    return out
