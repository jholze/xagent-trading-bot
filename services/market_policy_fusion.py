"""Global market bias fusion (Santiment sidecar + future oracles).

Arena collision rules (do not violate):
1. Size is applied ONCE in RiskManager via size_mult — not again here.
2. Never force MODE_DEFENSIVE from global RISK_OFF alone — GridStrategy
   sells 50% of every open position when mode is DEFENSIVE (churn).
3. Soft sentiment for RegimeDetector stays above allocator defensive_thresh
   (-0.55) for RISK_OFF so we bias weights without hard defensive override.
4. Mode changes only become more defensive: MOMENTUM→HYBRID; never strip GRID.
5. Grid spacing mult is additive on top of tier spacing (wider in risk-off).
"""

from __future__ import annotations

from typing import Any

from services.santiment_policy import get_santiment_policy


# Must stay > allocator defensive_sentiment_threshold (-0.55 default) for RISK_OFF.
_REGIME_SENTIMENT = {
    "RISK_ON": 0.55,
    "NEUTRAL": 0.0,
    "RISK_OFF": -0.45,
    "CRASH": -0.52,  # still soft; CRASH buys blocked via santiment_block
}


def get_global_market_bias(config_raw: dict | None = None) -> dict[str, Any]:
    """Single read for global bias layers (P3)."""
    from services.santiment_policy import santiment_risk_config

    san = get_santiment_policy(config_raw)
    cfg = santiment_risk_config(config_raw)
    regime = san.get("regime")
    active = bool(san.get("active"))
    sentiment = None
    if active and regime and cfg.get("inject_regime_sentiment", True):
        sentiment = float(_REGIME_SENTIMENT.get(str(regime).upper(), 0.0))
    spacing = 1.0
    if active and cfg.get("apply_grid_spacing", True):
        if regime == "CRASH":
            spacing = 1.45
        elif regime == "RISK_OFF":
            spacing = 1.25
    return {
        "active": active,
        "source": "santiment" if active else None,
        "regime": regime,
        "sentiment": sentiment,
        "size_mult": float(san.get("size_mult") or 1.0),
        "sensor_policy": san.get("sensor_policy") or "active",
        "block_buys": bool(san.get("block_buys")),
        "apply_size_mult": bool(san.get("apply_size_mult")),
        "apply_sensor_policy": bool(san.get("apply_sensor_policy")),
        "apply_mode_bias": active and bool(cfg.get("apply_mode_bias", True)),
        "apply_grid_spacing": active and bool(cfg.get("apply_grid_spacing", True)),
        "grid_spacing_mult": spacing,
        "rationale": san.get("rationale") or "",
        "as_of": san.get("as_of"),
        "fresh": bool(san.get("fresh")),
    }


def apply_global_mode_bias(
    mode: str,
    bias: dict[str, Any] | None = None,
    *,
    force_grid: bool = False,
) -> str:
    """Make mode more defensive under global risk-off — never more aggressive.

    Does NOT force DEFENSIVE (avoids GridStrategy inventory dump).
    """
    from strategies.trading_modes import (
        MODE_DEFENSIVE,
        MODE_GRID,
        MODE_HYBRID,
        MODE_MOMENTUM,
    )

    if not bias or not bias.get("apply_mode_bias"):
        return mode
    regime = str(bias.get("regime") or "").upper()
    if force_grid and mode == MODE_GRID:
        return MODE_GRID
    if regime == "CRASH":
        # Prefer HYBRID so grid sells/levels still run; buys blocked by Risk.
        if mode == MODE_MOMENTUM:
            return MODE_HYBRID
        return mode
    if regime == "RISK_OFF":
        if mode == MODE_MOMENTUM:
            return MODE_HYBRID
        return mode
    return mode


def inject_global_sentiment(social_context: dict | None, bias: dict[str, Any] | None = None) -> dict:
    """Merge global Santiment into regime social_context (P1)."""
    ctx = dict(social_context or {})
    bias = bias if bias is not None else get_global_market_bias()
    if bias.get("active") and bias.get("sentiment") is not None:
        # Do not overwrite a stronger coin-level santiment if ever set.
        ctx.setdefault("santiment_sentiment", float(bias["sentiment"]))
        ctx["santiment_regime"] = bias.get("regime")
        ctx["santiment_as_of"] = bias.get("as_of")
    return ctx
