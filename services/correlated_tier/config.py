"""Config helpers for sell_policy.correlated_tier — fail-closed when disabled."""

from __future__ import annotations

from typing import Any

_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "tenants": ["default", "henry"],
    "groups": {},
    "eval_interval_sec": 5,
    "flag_ttl_sec": 30,
    # Amplifiers (all off by default — never flip on here).
    "regime_amplify_enabled": False,
    "regime_amplify_regimes": ["RISK_OFF", "CRASH"],
    "news_pulse_enabled": False,
    "news_pulse_poll_interval_sec": 900,
    "news_pulse_since_minutes": 30,
    "news_pulse_bearish_threshold": 0.55,
    "news_pulse_min_confidence": 0.34,
    "news_pulse_max_combined_delta": 1,
}


def correlated_tier_config(raw: dict | None = None) -> dict[str, Any]:
    if raw is None:
        try:
            from core.config import get_bot_config

            raw = get_bot_config().raw
        except Exception:
            raw = {}
    sec: dict = {}
    if isinstance(raw, dict):
        sp = raw.get("sell_policy") or {}
        if isinstance(sp, dict):
            ct = sp.get("correlated_tier")
            if isinstance(ct, dict):
                sec = ct
        # also allow top-level for tests
        elif isinstance(raw.get("correlated_tier"), dict):
            sec = raw["correlated_tier"]
    out = {**_DEFAULTS, **sec}
    out["enabled"] = bool(out.get("enabled", False))
    try:
        out["eval_interval_sec"] = max(1.0, float(out.get("eval_interval_sec") or 5))
    except (TypeError, ValueError):
        out["eval_interval_sec"] = 5.0
    try:
        out["flag_ttl_sec"] = max(5, int(out.get("flag_ttl_sec") or 30))
    except (TypeError, ValueError):
        out["flag_ttl_sec"] = 30
    tenants = out.get("tenants") or []
    if not isinstance(tenants, list):
        tenants = []
    out["tenants"] = [str(t) for t in tenants]
    groups = out.get("groups") or {}
    if not isinstance(groups, dict):
        groups = {}
    out["groups"] = {str(k): dict(v) for k, v in groups.items() if isinstance(v, dict)}
    out["regime_amplify_enabled"] = bool(out.get("regime_amplify_enabled", False))
    out["news_pulse_enabled"] = bool(out.get("news_pulse_enabled", False))
    regimes = out.get("regime_amplify_regimes") or ["RISK_OFF", "CRASH"]
    if not isinstance(regimes, list):
        regimes = ["RISK_OFF", "CRASH"]
    out["regime_amplify_regimes"] = [str(r) for r in regimes]
    try:
        out["news_pulse_poll_interval_sec"] = max(
            30.0, float(out.get("news_pulse_poll_interval_sec") or 900)
        )
    except (TypeError, ValueError):
        out["news_pulse_poll_interval_sec"] = 900.0
    try:
        out["news_pulse_since_minutes"] = max(1, int(out.get("news_pulse_since_minutes") or 30))
    except (TypeError, ValueError):
        out["news_pulse_since_minutes"] = 30
    try:
        out["news_pulse_bearish_threshold"] = float(out.get("news_pulse_bearish_threshold") or 0.55)
    except (TypeError, ValueError):
        out["news_pulse_bearish_threshold"] = 0.55
    try:
        out["news_pulse_min_confidence"] = float(out.get("news_pulse_min_confidence") or 0.34)
    except (TypeError, ValueError):
        out["news_pulse_min_confidence"] = 0.34
    try:
        out["news_pulse_max_combined_delta"] = max(
            0, int(out.get("news_pulse_max_combined_delta") if out.get("news_pulse_max_combined_delta") is not None else 1)
        )
    except (TypeError, ValueError):
        out["news_pulse_max_combined_delta"] = 1
    return out


def correlated_tier_groups(raw: dict | None = None) -> dict[str, dict]:
    cfg = correlated_tier_config(raw)
    return dict(cfg.get("groups") or {})


def correlated_tier_enabled(raw: dict | None = None) -> bool:
    return bool(correlated_tier_config(raw).get("enabled"))


def enabled_proxy_symbols(raw: dict | None = None) -> list[str]:
    """Union of proxy_symbols across groups (top-level must be enabled)."""
    cfg = correlated_tier_config(raw)
    if not cfg.get("enabled"):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for g in (cfg.get("groups") or {}).values():
        if not isinstance(g, dict) or g.get("enabled") is False:
            continue
        for s in g.get("proxy_symbols") or []:
            sym = str(s or "").strip().upper().replace("-", "/")
            if not sym or sym in seen:
                continue
            seen.add(sym)
            out.append(sym)
    return out
