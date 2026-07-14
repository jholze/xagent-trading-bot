"""Tenant trading profile presets (risk + coin filters) and config merge."""

from __future__ import annotations

import copy
from typing import Any

VALID_PROFILES = frozenset({"conservative", "balanced", "aggressive"})
DEFAULT_PROFILE = "balanced"

_COIN_FILTER_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "min_market_cap_usd": 5_000_000,
    "max_market_cap_usd": None,
    "max_atr_pct": None,
    "min_atr_pct": None,
    "block_coin_classes": [],
    "block_volatility_tiers": [],
    "block_sources": [],
    "allow_trending_watchlist": True,
    "new_buys_stable_only": False,
    "require_known_market_cap": False,
    "prefer_volatile": False,
}

TRADING_PROFILE_PRESETS: dict[str, dict[str, Any]] = {
    "conservative": {
        "trading_profile": "conservative",
        "max_open_positions": 4,
        "max_usdt_per_trade": 80,
        "stop_loss_pct": 8.0,
        "max_daily_trades": 3,
        "trade_cooldown_hours": 2.0,
        "coin_filters": {
            "min_market_cap_usd": 50_000_000,
            "max_atr_pct": 4.0,
            "block_coin_classes": ["meme"],
            "block_volatility_tiers": ["volatile"],
            "block_sources": ["cmc_trending"],
            "allow_trending_watchlist": False,
            "new_buys_stable_only": True,
        },
    },
    "balanced": {
        "trading_profile": "balanced",
        "max_open_positions": 6,
        "max_usdt_per_trade": 150,
        "stop_loss_pct": 12.0,
        "max_daily_trades": 5,
        "trade_cooldown_hours": 1.0,
        "coin_filters": {
            "min_market_cap_usd": 10_000_000,
            "max_atr_pct": 8.0,
            "block_coin_classes": ["meme"],
            "block_volatility_tiers": [],
            "block_sources": [],
            "allow_trending_watchlist": True,
            "new_buys_stable_only": False,
        },
    },
    "aggressive": {
        "trading_profile": "aggressive",
        "max_open_positions": 10,
        "max_usdt_per_trade": 250,
        "stop_loss_pct": 15.0,
        "max_daily_trades": 8,
        "trade_cooldown_hours": 0.5,
        "coin_filters": {
            "min_market_cap_usd": 2_000_000,
            "max_atr_pct": None,
            "min_atr_pct": 5.0,
            "block_coin_classes": [],
            "block_volatility_tiers": ["stable"],
            "block_sources": [],
            "allow_trending_watchlist": True,
            "new_buys_stable_only": False,
            "prefer_volatile": True,
        },
    },
}


def deep_merge_dicts(base: dict, overlay: dict | None) -> dict:
    """Recursively merge overlay into a copy of base (dict values merge, scalars replace)."""
    if not overlay:
        return copy.deepcopy(base)
    out = copy.deepcopy(base)
    for key, val in overlay.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge_dicts(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


def normalize_profile_name(name: str | None) -> str | None:
    if not name:
        return None
    key = str(name).strip().lower()
    return key if key in VALID_PROFILES else None


def resolve_profile_name(base_cfg: dict, tenant_overrides: dict | None = None) -> str | None:
    if tenant_overrides:
        explicit = normalize_profile_name(tenant_overrides.get("trading_profile"))
        if explicit:
            return explicit
    return normalize_profile_name((base_cfg or {}).get("trading_profile"))


def coin_filters_config(cfg: dict | None) -> dict:
    """Effective coin_filters with defaults."""
    raw = (cfg or {}).get("coin_filters") or {}
    return {**_COIN_FILTER_DEFAULTS, **raw}


def apply_effective_config(base_cfg: dict, tenant_overrides: dict | None = None) -> dict:
    """Merge: config.json baseline → profile preset → tenant overrides."""
    merged = copy.deepcopy(base_cfg or {})
    profile = resolve_profile_name(merged, tenant_overrides)
    if profile:
        preset = TRADING_PROFILE_PRESETS.get(profile, {})
        merged = deep_merge_dicts(merged, preset)
    if tenant_overrides:
        merged = deep_merge_dicts(merged, tenant_overrides)
    return merged


def build_tenant_seed_config(
    profile: str = DEFAULT_PROFILE,
    *,
    trading_mode: str = "paper",
    extra: dict | None = None,
) -> dict:
    """Config body to persist for a new tenant (profile + optional overrides)."""
    name = normalize_profile_name(profile) or DEFAULT_PROFILE
    body: dict[str, Any] = {
        "trading_mode": trading_mode,
        "virtual_trading": True,
        "trading_profile": name,
    }
    preset = TRADING_PROFILE_PRESETS.get(name, {})
    for key in ("max_open_positions", "max_usdt_per_trade", "stop_loss_pct"):
        if key in preset:
            body[key] = preset[key]
    if extra:
        body.update(extra)
    return body