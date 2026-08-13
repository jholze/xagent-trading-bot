"""Correlated-tier trail overlay — no new volatility classifier tier.

Applies per-group trailing_take_profit / trailing_stop overlays when
sell_policy.correlated_tier is enabled and the symbol resolves to a group.
"""

from __future__ import annotations

from typing import Any


def _norm_symbol(symbol: str | None) -> str:
    s = str(symbol or "").strip().upper().replace("-", "/")
    if "_" in s and "/" not in s:
        a, b = s.rsplit("_", 1)
        s = f"{a}/{b}"
    return s


def _ct_root(config_raw: dict | None) -> dict:
    return dict((config_raw or {}).get("sell_policy") or {}).get("correlated_tier") or {}


def _groups(config_raw: dict | None) -> dict[str, dict]:
    root = _ct_root(config_raw)
    groups = root.get("groups") or {}
    if not isinstance(groups, dict):
        return {}
    return {str(k): dict(v) for k, v in groups.items() if isinstance(v, dict)}


def resolve_correlated_group(symbol: str, config_raw: dict | None) -> str | None:
    """Map symbol → group name.

    Explicit membership wins (most specific). ``*`` groups claim any open
    position not listed elsewhere and not itself a proxy_symbol of any group.
    """
    sym = _norm_symbol(symbol)
    if not sym:
        return None
    groups = _groups(config_raw)
    if not groups:
        return None

    explicit_hit: str | None = None
    star_group: str | None = None
    all_proxies: set[str] = set()

    for name, g in groups.items():
        proxies = {_norm_symbol(x) for x in (g.get("proxy_symbols") or []) if x}
        all_proxies |= proxies
        if g.get("enabled") is False:
            continue
        members = g.get("member_symbols")
        if members == "*" or members == ["*"]:
            if star_group is None:
                star_group = name
            continue
        if not isinstance(members, list):
            continue
        member_set = {_norm_symbol(x) for x in members if x}
        if sym in member_set:
            # first explicit match wins; later groups do not override
            if explicit_hit is None:
                explicit_hit = name

    if explicit_hit is not None:
        return explicit_hit

    # Proxies are detectors, not members of their own (or any) * group
    if sym in all_proxies:
        return None

    return star_group


def apply_correlated_tier_overlay(
    params: dict,
    symbol: str,
    config_raw: dict | None,
) -> dict:
    """Overlay group trail knobs onto resolved strategy params. Fail-open no-op."""
    try:
        root = _ct_root(config_raw)
        if not root.get("enabled"):
            return params
        group_name = resolve_correlated_group(symbol, config_raw)
        if not group_name:
            return params
        group = _groups(config_raw).get(group_name) or {}
        if group.get("enabled") is False:
            return params

        out = dict(params or {})
        for key in ("trailing_take_profit", "trailing_stop"):
            sub = group.get(key)
            if not isinstance(sub, dict) or not sub:
                continue
            base = dict(out.get(key) or {})
            base.update(sub)
            # Honor an explicit trail_pct overlay instead of leaving dynamic_trail on.
            if key == "trailing_take_profit" and "trail_pct" in sub and "dynamic_trail" not in sub:
                base["dynamic_trail"] = False
            out[key] = base

        # Group-level full-close threshold rides on trailing_take_profit
        if group.get("full_close_gain_pct") is not None:
            ttp = dict(out.get("trailing_take_profit") or {})
            try:
                ttp["full_close_gain_pct"] = float(group["full_close_gain_pct"])
            except (TypeError, ValueError):
                pass
            else:
                out["trailing_take_profit"] = ttp

        out["correlated_tier_group"] = group_name
        return out
    except Exception:
        return params


def correlated_tier_group_config(
    group_name: str, config_raw: dict | None
) -> dict[str, Any]:
    return dict(_groups(config_raw).get(group_name) or {})
