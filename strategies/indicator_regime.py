"""Single overlay for regime RSI + trail_exclusive RSI punch-through.

Layer order (do not add a fifth sell overlay):
  1. position lock / recovery_hold
  2. oracle_climax grind|harvest (BB/TTP/TS block; BTC dump full-close)
  3. trail_exclusive (structure/social/generic technical) EXCEPT rsi_sell allowlist
  4. THIS module: RSI *thresholds* + force SELL_FULL on rsi sources
  5. rotation profit_full_close — rsi_sell is already FULL so it does not rewrite

Kill: sell_policy.indicator_regime.enabled=false
      (or trail_allow_rsi=false to keep deltas but restore exclusive block).
Tenants: sell_policy.indicator_regime.tenants (empty = all). Staging: default+henry.
"""

from __future__ import annotations

from typing import Any

from core.actions import SELL_FULL
from core.tenant_context import resolve_tenant_id
from strategies.oracle_climax import (
    MODE_GRIND,
    MODE_HARVEST,
    MODE_IDLE,
    MODE_TIGHTEN,
    current_climax,
)
from strategies.sell_sources import RSI_SELL_SOURCE, TRAIL_ALLOW_RSI_SOURCES

_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "trail_allow_rsi": True,
    "rsi_full_close": True,
    "tenants": ["default", "henry"],
    "rsi_sell_30_min": 55.0,
    "rsi_sell_30_max": 82.0,
    "rsi_sell_20_min": 65.0,
    "rsi_sell_20_max": 92.0,
    "by_mode": {
        MODE_GRIND: {
            "rsi_sell_30_delta": 8.0,
            "rsi_sell_20_delta": 8.0,
            "rsi_sell_min_gain_delta": 3.0,
            "rollover_peak_rsi": 80.0,
            "rollover_current_max": 68.0,
            "rollover_min_gain": 12.0,
        },
        MODE_TIGHTEN: {
            "rsi_sell_30_delta": -4.0,
            "rsi_sell_20_delta": -4.0,
            "rsi_sell_min_gain_delta": 0.0,
            "rollover_peak_rsi": 68.0,
            "rollover_current_max": 58.0,
            "rollover_min_gain": 7.0,
        },
        MODE_HARVEST: {
            "rsi_sell_30_delta": -8.0,
            "rsi_sell_20_delta": -8.0,
            "rsi_sell_min_gain_delta": -5.0,
            "rollover_peak_rsi": 65.0,
            "rollover_current_max": 55.0,
            "rollover_min_gain": 5.0,
        },
        MODE_IDLE: {},
    },
    "fusion_mode_alias": {
        "RISK_OFF": MODE_HARVEST,
        "CRASH": MODE_HARVEST,
    },
}


def _config_raw(config_raw: dict | None) -> dict:
    if config_raw is not None:
        return config_raw
    try:
        from core.config import get_bot_config

        return get_bot_config().raw or {}
    except Exception:
        return {}


def indicator_regime_config(config_raw: dict | None) -> dict:
    block = dict((_config_raw(config_raw).get("sell_policy") or {}).get("indicator_regime") or {})
    out = {**_DEFAULTS, **block}
    by_mode = dict(_DEFAULTS["by_mode"])
    raw_modes = block.get("by_mode") or {}
    if isinstance(raw_modes, dict):
        for key, val in raw_modes.items():
            by_mode[str(key)] = {**(by_mode.get(str(key)) or {}), **(val or {})}
    out["by_mode"] = by_mode
    fusion_alias = dict(_DEFAULTS["fusion_mode_alias"])
    raw_alias = block.get("fusion_mode_alias") or {}
    if isinstance(raw_alias, dict):
        fusion_alias.update({str(k).upper(): str(v) for k, v in raw_alias.items()})
    out["fusion_mode_alias"] = fusion_alias
    tenants = out.get("tenants")
    if tenants is None:
        out["tenants"] = list(_DEFAULTS["tenants"])
    elif isinstance(tenants, str):
        out["tenants"] = [t.strip() for t in tenants.split(",") if t.strip()]
    else:
        out["tenants"] = [str(t).strip() for t in tenants if str(t).strip()]
    return out


def _tenant_ok(cfg: dict, tenant_id: str | None = None) -> bool:
    allowed = cfg.get("tenants") or []
    if not allowed:
        return True
    return resolve_tenant_id(tenant_id) in {str(t) for t in allowed}


def overlay_active(config_raw: dict | None, *, tenant_id: str | None = None) -> bool:
    cfg = indicator_regime_config(config_raw)
    return bool(cfg.get("enabled")) and _tenant_ok(cfg, tenant_id)


def trail_allow_rsi(config_raw: dict | None, *, tenant_id: str | None = None) -> bool:
    cfg = indicator_regime_config(config_raw)
    return overlay_active(config_raw, tenant_id=tenant_id) and bool(cfg.get("trail_allow_rsi"))


def rsi_full_close(config_raw: dict | None, *, tenant_id: str | None = None) -> bool:
    cfg = indicator_regime_config(config_raw)
    return overlay_active(config_raw, tenant_id=tenant_id) and bool(cfg.get("rsi_full_close"))


def _fusion_regime(config_raw: dict | None) -> str:
    try:
        from services.market_policy_fusion import get_global_market_bias

        return str((get_global_market_bias(config_raw) or {}).get("regime") or "").upper()
    except Exception:
        return ""


def resolve_indicator_mode(
    config_raw: dict | None = None,
    *,
    climax_mode: str | None = None,
    fusion_regime: str | None = None,
) -> str:
    """Climax mode wins; Fusion RISK_OFF/CRASH aliases to harvest. No double-stack."""
    cfg = indicator_regime_config(config_raw)
    fusion = (fusion_regime if fusion_regime is not None else _fusion_regime(config_raw) or "").upper()
    alias = (cfg.get("fusion_mode_alias") or {}).get(fusion)
    if alias:
        return str(alias)
    if climax_mode:
        return str(climax_mode)
    try:
        dec, _oc = current_climax(config_raw)
        return str(dec.mode or MODE_IDLE)
    except Exception:
        return MODE_IDLE


def _mode_knobs(cfg: dict, mode: str) -> dict:
    by_mode = cfg.get("by_mode") or {}
    return dict(by_mode.get(mode) or {})


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(float(lo), min(float(hi), float(val)))


def apply_rsi_sell_overlay(
    params: dict,
    config_raw: dict | None = None,
    *,
    climax_mode: str | None = None,
    fusion_regime: str | None = None,
) -> dict:
    """Copy strategy_params with RSI sell knobs shifted. Idle / disabled = identity."""
    out = dict(params or {})
    if not overlay_active(config_raw):
        return out
    cfg = indicator_regime_config(config_raw)
    mode = resolve_indicator_mode(
        config_raw, climax_mode=climax_mode, fusion_regime=fusion_regime
    )
    knobs = _mode_knobs(cfg, mode)
    if not knobs:
        return out
    d30 = float(knobs.get("rsi_sell_30_delta") or 0)
    d20 = float(knobs.get("rsi_sell_20_delta") or 0)
    dmin = float(knobs.get("rsi_sell_min_gain_delta") or 0)
    if d30:
        out["rsi_sell_30"] = _clamp(
            float(out.get("rsi_sell_30") or 68) + d30,
            float(cfg["rsi_sell_30_min"]),
            float(cfg["rsi_sell_30_max"]),
        )
    if d20:
        out["rsi_sell_20"] = _clamp(
            float(out.get("rsi_sell_20") or 78) + d20,
            float(cfg["rsi_sell_20_min"]),
            float(cfg["rsi_sell_20_max"]),
        )
        if float(out["rsi_sell_20"]) < float(out.get("rsi_sell_30") or 0) + 6:
            out["rsi_sell_20"] = float(out.get("rsi_sell_30") or 0) + 6
    if dmin:
        out["rsi_sell_min_gain_pct"] = max(
            0.0, float(out.get("rsi_sell_min_gain_pct") or 15) + dmin
        )
    return out


def apply_rollover_overlay(
    rollover_cfg: dict,
    config_raw: dict | None = None,
    *,
    climax_mode: str | None = None,
    fusion_regime: str | None = None,
) -> dict:
    out = dict(rollover_cfg or {})
    if not overlay_active(config_raw):
        return out
    cfg = indicator_regime_config(config_raw)
    mode = resolve_indicator_mode(
        config_raw, climax_mode=climax_mode, fusion_regime=fusion_regime
    )
    knobs = _mode_knobs(cfg, mode)
    if knobs.get("rollover_peak_rsi") is not None:
        out["peak_rsi_min"] = float(knobs["rollover_peak_rsi"])
    if knobs.get("rollover_current_max") is not None:
        out["current_rsi_max"] = float(knobs["rollover_current_max"])
    if knobs.get("rollover_min_gain") is not None:
        out["min_gain_pct"] = float(knobs["rollover_min_gain"])
    return out


def is_rsi_level_action(action: str | None) -> bool:
    act = str(action or "").upper()
    return act in {
        "SELL_20",
        "SELL_30",
        "SELL_PARTIAL_20",
        "SELL_PARTIAL_30",
        SELL_FULL,
    } and "STOP" not in act and "TP" not in act


def relabel_technical_as_rsi_sell(
    *,
    action: str,
    source: str,
    technical_sources: list | None,
    config_raw: dict | None,
) -> tuple[str, str]:
    """Map cycle TA RSI partials → rsi_sell (+ optional SELL_FULL). Leave TP/SL alone."""
    src = (source or "").lower()
    if src != "technical":
        return action, source
    srcs = [str(s).lower() for s in (technical_sources or [])]
    if any("take_profit" in s or s == "stop_loss" for s in srcs):
        return action, source
    act_u = str(action or "").upper()
    if act_u not in {
        "SELL_20",
        "SELL_30",
        "SELL_PARTIAL_20",
        "SELL_PARTIAL_30",
        "SELL",
    }:
        return action, source
    if not trail_allow_rsi(config_raw):
        return action, source
    out_action = SELL_FULL if rsi_full_close(config_raw) else action
    return out_action, RSI_SELL_SOURCE


def normalize_rsi_candidates(
    candidates: list[tuple],
    config_raw: dict | None,
) -> list[tuple]:
    """Force FULL on allowlisted RSI sources. No-op if overlay off."""
    if not rsi_full_close(config_raw):
        return candidates
    out = []
    for item in candidates:
        action, priority, source = item[0], item[1], item[2]
        src = str(source or "").lower()
        if src in TRAIL_ALLOW_RSI_SOURCES:
            out.append((SELL_FULL, max(int(priority), 5), source))
        else:
            out.append(item)
    return out
