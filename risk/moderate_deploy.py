"""Moderate deploy unlock — slightly larger auto sizes when not RISK_OFF.

Goal: avoid both extremes
  - restart sprint (all slots full in a few hours)
  - cash parking (~15% deployed after 1–2 days)

Rollback: risk.moderate_deploy.enabled=false (and restore universe.trade_max_coins).
"""

from __future__ import annotations

from typing import Any

_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "size_boost_risk_on": 1.55,
    "size_boost_neutral": 1.5,
    "size_boost_risk_off": 1.0,
    "size_boost_crash": 1.0,
    "size_boost_warmup": 1.0,
    "size_boost_default": 1.0,
    # Allow total size mult slightly above aggression.max_position_multiplier when boosting
    "max_total_multiplier": 1.6,
    "apply_to_dca": True,
    # DCA gets a milder lift: 1 + (boost-1)*scale  (1.5 with 0.7 → 1.35)
    "dca_boost_scale": 0.7,
    "max_boost": 1.75,
    # When cash is a large share of equity, deploy harder (anti cash-parking)
    "cash_rich_pct": 55.0,
    "cash_rich_extra_mult": 1.25,
}


def moderate_deploy_config(config: dict | None = None) -> dict[str, Any]:
    raw: dict[str, Any] = {}
    if isinstance(config, dict):
        risk = config.get("risk")
        if isinstance(risk, dict) and isinstance(risk.get("moderate_deploy"), dict):
            raw = dict(risk["moderate_deploy"])
        elif isinstance(config.get("moderate_deploy"), dict):
            raw = dict(config["moderate_deploy"])
    out = {**_DEFAULTS, **raw}
    out["enabled"] = bool(out.get("enabled", False))
    out["apply_to_dca"] = bool(out.get("apply_to_dca", True))
    for k in (
        "size_boost_risk_on",
        "size_boost_neutral",
        "size_boost_risk_off",
        "size_boost_crash",
        "size_boost_warmup",
        "size_boost_default",
        "max_total_multiplier",
        "dca_boost_scale",
        "max_boost",
        "cash_rich_pct",
        "cash_rich_extra_mult",
    ):
        try:
            out[k] = float(out.get(k) if out.get(k) is not None else _DEFAULTS[k])
        except (TypeError, ValueError):
            out[k] = float(_DEFAULTS[k])
    out["dca_boost_scale"] = max(0.0, min(1.0, float(out["dca_boost_scale"])))
    out["max_boost"] = max(1.0, float(out["max_boost"]))
    out["max_total_multiplier"] = max(1.0, float(out["max_total_multiplier"]))
    out["cash_rich_pct"] = max(0.0, min(100.0, float(out["cash_rich_pct"])))
    out["cash_rich_extra_mult"] = max(1.0, float(out["cash_rich_extra_mult"]))
    return out


def moderate_deploy_enabled(config: dict | None = None) -> bool:
    return bool(moderate_deploy_config(config).get("enabled"))


def _normalize_regime(regime: str | None) -> str:
    r = str(regime or "").strip().upper()
    if r in ("RISK_ON", "RISKON", "ON"):
        return "RISK_ON"
    if r in ("NEUTRAL", "NORM", "NORMAL"):
        return "NEUTRAL"
    if r in ("RISK_OFF", "RISKOFF", "OFF"):
        return "RISK_OFF"
    if r in ("CRASH", "PANIC"):
        return "CRASH"
    if r in ("WARMUP", "WARM_UP"):
        return "WARMUP"
    return r or "UNKNOWN"


def size_boost_for_regime(
    config: dict | None,
    regime: str | None,
    *,
    is_dca: bool = False,
    cash_pct: float | None = None,
) -> float:
    """Return size multiplier ≥1.0 (1.0 = no change). Fail-open → 1.0.

    cash_pct: cash as % of equity (0–100). When above cash_rich_pct, apply extra mult
    so parked capital deploys faster without lowering exit rules.
    """
    try:
        cfg = moderate_deploy_config(config)
        if not cfg.get("enabled"):
            return 1.0
        if is_dca and not cfg.get("apply_to_dca", True):
            return 1.0

        reg = _normalize_regime(regime)
        key = {
            "RISK_ON": "size_boost_risk_on",
            "NEUTRAL": "size_boost_neutral",
            "RISK_OFF": "size_boost_risk_off",
            "CRASH": "size_boost_crash",
            "WARMUP": "size_boost_warmup",
        }.get(reg, "size_boost_default")
        boost = float(cfg.get(key) or 1.0)
        if boost < 1.0:
            boost = 1.0
        if is_dca and boost > 1.0:
            scale = float(cfg.get("dca_boost_scale") or 0.7)
            boost = 1.0 + (boost - 1.0) * scale
        # Cash parking antidote (skip on CRASH)
        if (
            reg != "CRASH"
            and cash_pct is not None
            and float(cash_pct) >= float(cfg.get("cash_rich_pct") or 55.0)
        ):
            boost *= float(cfg.get("cash_rich_extra_mult") or 1.25)
        boost = min(float(cfg.get("max_boost") or 1.75), boost)
        return boost
    except Exception:
        return 1.0


def effective_max_total_multiplier(
    config: dict | None,
    *,
    base_max: float,
    boost: float,
) -> float:
    """When boosting, allow a slightly higher total mult ceiling."""
    try:
        if boost <= 1.001:
            return float(base_max)
        cfg = moderate_deploy_config(config)
        md_max = float(cfg.get("max_total_multiplier") or base_max)
        return max(float(base_max), md_max)
    except Exception:
        return float(base_max)
