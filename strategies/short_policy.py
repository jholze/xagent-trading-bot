"""Resolve short params: tier defaults → per-coin overlay → lot override."""

from __future__ import annotations

from typing import Any

from strategies.short_math import clamp_leverage

AUTO_SOURCES = (
    "rsi_sell",
    "exit_1h_rsi_rollover",
    "oracle_climax_harvest",
    "exit_volume_climax",
)

_VOLATILE_KEYS = ("volatile", "volatile_altcoin", "meme")
_STABLE_KEYS = ("stable", "stable_altcoin", "large_cap", "normal")


def shorts_config(raw: dict | None = None) -> dict[str, Any]:
    if raw is None:
        try:
            from core.config import get_bot_config

            raw = get_bot_config().raw
        except Exception:
            raw = {}
    block = (raw or {}).get("shorts") if isinstance(raw, dict) else None
    return dict(block) if isinstance(block, dict) else {}


def shorts_enabled(raw: dict | None = None) -> bool:
    return bool(shorts_config(raw).get("enabled"))


def shorts_allow_live(raw: dict | None = None) -> bool:
    return bool(shorts_config(raw).get("allow_live"))


def is_auto_short_source(source: str | None, raw: dict | None = None) -> bool:
    allow = shorts_config(raw).get("auto_sources") or AUTO_SOURCES
    s = str(source or "").strip()
    return s in set(allow)


def _tier_key(tier: str | None) -> str:
    t = str(tier or "volatile").strip().lower()
    if t in _STABLE_KEYS:
        return "stable"
    return "volatile"


def resolve_short_params(
    *,
    symbol: str | None = None,
    tier: str | None = None,
    lot: dict | None = None,
    config_raw: dict | None = None,
) -> dict[str, Any]:
    cfg = shorts_config(config_raw)
    cap = float(cfg.get("leverage_cap") or 5)
    vol = dict(cfg.get("volatile") or {})
    st = dict(cfg.get("stable") or {})
    base = {
        "time_cap_hours": 4.0,
        "stop_margin_pct": 0.12,
        "market_cap_min_usd": 50_000_000,
        "trail_arm_pct": 4.0,
        "trail_retrace_pct": 1.5,
        "rsi_cover_below": 32.0,
        "funding_rate_8h": float(cfg.get("funding_rate_8h") or 0.0001),
        "leverage": float(cfg.get("leverage_default") or 2),
        "max_open": int(cfg.get("max_open") or 6),
        "max_margin_pct": float(cfg.get("max_margin_pct") or 20),
        "liquidation_buffer": float(cfg.get("liquidation_buffer") or 0.05),
        "fee_rate": float(cfg.get("fee_rate") or 0.001),
    }
    tier_key = _tier_key(tier or (lot or {}).get("strategy_tier"))
    overlay = st if tier_key == "stable" else vol
    for k, v in overlay.items():
        if v is not None:
            base[k] = v
    coins = cfg.get("coins") if isinstance(cfg.get("coins"), dict) else {}
    sym = str(symbol or (lot or {}).get("symbol") or "").upper()
    coin_over = coins.get(sym) or coins.get(sym.replace("/USDT", "") + "/USDT")
    if isinstance(coin_over, dict):
        for k, v in coin_over.items():
            if v is not None:
                base[k] = v
    lot_lev = (lot or {}).get("leverage") if isinstance(lot, dict) else None
    if lot_lev:
        base["leverage"] = lot_lev
    base["leverage"] = clamp_leverage(base.get("leverage") or 2, cap=cap)
    base["leverage_cap"] = cap
    base["tier"] = tier_key
    base["enabled"] = bool(cfg.get("enabled"))
    return base
