"""Global market bias fusion (Market Oracle + Santiment).

Arena collision rules:
1. Size applied ONCE in RiskManager — fusion takes min(size_mult).
2. Never force MODE_DEFENSIVE from global RISK_OFF (Grid 50% dump).
3. Soft sentiment for RegimeDetector stays > allocator defensive_thresh (-0.55)
   for RISK_OFF.
4. Mode only becomes more defensive: MOMENTUM→HYBRID.
5. Grid spacing: max of source multipliers (wider wins).
6. sensor_policy severity: block > shadow > active.
"""

from __future__ import annotations

from typing import Any

from services.market_oracle_policy import get_market_oracle_policy
from services.santiment_policy import get_santiment_policy, santiment_risk_config

_REGIME_SENTIMENT = {
    "RISK_ON": 0.55,
    "NEUTRAL": 0.0,
    "RISK_OFF": -0.45,
    "CRASH": -0.52,
    "WARMUP": -0.3,
}

_SEVERITY = {"active": 0, "shadow": 1, "block": 2}
_STATE_RANK = {"RISK_ON": 0, "NEUTRAL": 1, "WARMUP": 2, "RISK_OFF": 3, "CRASH": 4}


def _worse_regime(a: str | None, b: str | None) -> str | None:
    if not a:
        return b
    if not b:
        return a
    ra, rb = _STATE_RANK.get(a.upper(), 1), _STATE_RANK.get(b.upper(), 1)
    return a if ra >= rb else b


def _worse_sensor(a: str, b: str) -> str:
    return a if _SEVERITY.get(a, 0) >= _SEVERITY.get(b, 0) else b


def get_global_market_bias(config_raw: dict | None = None) -> dict[str, Any]:
    """Merge oracle + santiment into one policy for the bot."""
    san = get_santiment_policy(config_raw)
    ora = get_market_oracle_policy(config_raw)
    san_cfg = santiment_risk_config(config_raw)

    layers = []
    if san.get("active"):
        layers.append(("santiment", san))
    if ora.get("active"):
        layers.append(("oracle", ora))

    if not layers:
        return {
            "active": False,
            "source": None,
            "sources": [],
            "regime": None,
            "sentiment": None,
            "size_mult": 1.0,
            "sensor_policy": "active",
            "block_buys": False,
            "apply_size_mult": False,
            "apply_sensor_policy": False,
            "apply_mode_bias": False,
            "apply_grid_spacing": False,
            "grid_spacing_mult": 1.0,
            "rationale": "no global bias active",
            "as_of": None,
            "fresh": False,
            "warmup_active": False,
        }

    regime = None
    size = 1.0
    sensor = "active"
    block = False
    rationales = []
    sources = []
    as_ofs = []
    apply_size = False
    apply_sensor = False

    for name, pol in layers:
        sources.append(name)
        regime = _worse_regime(regime, pol.get("regime") or pol.get("state"))
        if pol.get("apply_size_mult"):
            apply_size = True
            size = min(size, float(pol.get("size_mult") or 1.0))
        if pol.get("apply_sensor_policy"):
            apply_sensor = True
            sensor = _worse_sensor(sensor, str(pol.get("sensor_policy") or "active"))
        block = block or bool(pol.get("block_buys"))
        if pol.get("rationale"):
            rationales.append(f"{name}:{pol['rationale']}")
        if pol.get("as_of"):
            as_ofs.append(str(pol["as_of"]))

    spacing = 1.0
    if regime == "CRASH":
        spacing = 1.45
    elif regime in ("RISK_OFF", "WARMUP"):
        spacing = 1.25

    inject = bool(san_cfg.get("inject_regime_sentiment", True))
    sentiment = float(_REGIME_SENTIMENT.get(str(regime or "NEUTRAL").upper(), 0.0)) if inject else None

    return {
        "active": True,
        "source": "+".join(sources),
        "sources": sources,
        "regime": regime,
        "sentiment": sentiment,
        "size_mult": max(0.0, min(1.5, size)),
        "sensor_policy": sensor,
        "block_buys": block,
        "apply_size_mult": apply_size,
        "apply_sensor_policy": apply_sensor,
        "apply_mode_bias": True,
        "apply_grid_spacing": True,
        "grid_spacing_mult": spacing,
        "rationale": " | ".join(rationales),
        "as_of": max(as_ofs) if as_ofs else None,
        "fresh": True,
        "warmup_active": bool(ora.get("warmup_active")),
    }


def apply_global_mode_bias(
    mode: str,
    bias: dict[str, Any] | None = None,
    *,
    force_grid: bool = False,
) -> str:
    from strategies.trading_modes import MODE_GRID, MODE_HYBRID, MODE_MOMENTUM

    if not bias or not bias.get("apply_mode_bias"):
        return mode
    regime = str(bias.get("regime") or "").upper()
    if force_grid and mode == MODE_GRID:
        return MODE_GRID
    if regime in ("CRASH", "RISK_OFF", "WARMUP"):
        if mode == MODE_MOMENTUM:
            return MODE_HYBRID
        return mode
    return mode


def inject_global_sentiment(social_context: dict | None, bias: dict[str, Any] | None = None) -> dict:
    ctx = dict(social_context or {})
    bias = bias if bias is not None else get_global_market_bias()
    if bias.get("active") and bias.get("sentiment") is not None:
        ctx.setdefault("santiment_sentiment", float(bias["sentiment"]))
        # also expose as market bias for future detectors
        ctx.setdefault("market_oracle_sentiment", float(bias["sentiment"]))
        ctx["global_regime"] = bias.get("regime")
        ctx["santiment_regime"] = bias.get("regime")
        ctx["global_bias_as_of"] = bias.get("as_of")
    return ctx
