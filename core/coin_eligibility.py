"""Per-profile coin eligibility checks for buys and watchlist filtering."""

from __future__ import annotations

from typing import Any

from core.models import MarketContext
from core.trading_profiles import coin_filters_config
from intelligence.strategy_backtest import classify_coin
from intelligence.volatility_classifier import volatility_tier


def _coin_class(coin: dict, market: MarketContext | None) -> str:
    params = (market.strategy_params if market else None) or coin.get("strategy_params") or {}
    return classify_coin(coin.get("symbol", ""), params)


def _vol_tier(coin: dict, market: MarketContext | None, cfg: dict) -> str:
    params = (market.strategy_params if market else None) or coin.get("strategy_params") or {}
    frozen = params.get("volatility_tier")
    if frozen in ("stable", "volatile"):
        return str(frozen)
    atr = float((market.atr_pct if market else None) or coin.get("atr_pct") or params.get("atr_pct") or 3.0)
    va_cfg = (cfg or {}).get("volatile_altcoin") or {}
    return volatility_tier(coin, atr, va_cfg, frozen_tier=frozen)


def _atr_pct(coin: dict, market: MarketContext | None) -> float:
    if market is not None:
        return float(market.atr_pct)
    params = coin.get("strategy_params") or {}
    return float(coin.get("atr_pct") or params.get("atr_pct") or 3.0)


def passes_coin_filters(
    coin: dict,
    market: MarketContext | None,
    cfg: dict,
    *,
    context: str = "buy",
) -> tuple[bool, str]:
    """Return (ok, reason). context: 'buy' | 'watchlist'."""
    symbol = coin.get("symbol", "") or (market.symbol if market else "")
    if not symbol:
        return False, "missing symbol"

    # Permanent stablecoin rail (GUSD/USDP/USDC/…) — not volatility_tier "stable".
    # Independent of coin_filters.enabled so it cannot be accidentally disabled.
    if context == "buy":
        try:
            from core.stablecoins import (
                is_stablecoin_symbol,
                stablecoin_block_reason,
                stablecoin_buys_blocked,
            )

            if stablecoin_buys_blocked(cfg) and is_stablecoin_symbol(symbol):
                return False, stablecoin_block_reason(symbol)
        except Exception:
            pass

    filters = coin_filters_config(cfg)
    if not filters.get("enabled", True):
        return True, ""

    from data.cmc_market_cap import passes_market_cap_filter, resolve_market_cap_usd

    mcap = resolve_market_cap_usd(symbol, coin)
    require_known = bool(filters.get("require_known_market_cap"))
    if context == "buy":
        require_known = require_known or bool(filters.get("new_buys_stable_only"))

    mcap_ok, mcap_reason = passes_market_cap_filter(mcap, filters, require_known=require_known)
    if not mcap_ok:
        return False, mcap_reason or "market cap filter"

    atr = _atr_pct(coin, market)
    max_atr = filters.get("max_atr_pct")
    if max_atr is not None and atr > float(max_atr):
        return False, f"ATR {atr:.1f}% > max {float(max_atr):.1f}%"

    min_atr = filters.get("min_atr_pct")
    if min_atr is not None and atr < float(min_atr):
        return False, f"ATR {atr:.1f}% < min {float(min_atr):.1f}%"

    coin_class = _coin_class(coin, market)
    blocked_classes = list(filters.get("block_coin_classes") or [])
    if coin_class in blocked_classes:
        return False, f"coin class '{coin_class}' blocked"

    tier = _vol_tier(coin, market, cfg)
    blocked_tiers = list(filters.get("block_volatility_tiers") or [])
    if tier in blocked_tiers:
        return False, f"volatility tier '{tier}' blocked"

    source = str(coin.get("source") or "")
    blocked_sources = list(filters.get("block_sources") or [])
    if source and source in blocked_sources:
        return False, f"source '{source}' blocked"

    if context == "buy" and filters.get("new_buys_stable_only") and tier != "stable":
        return False, "new buys require stable volatility tier"

    prefer_volatile = bool(filters.get("prefer_volatile"))
    if context == "watchlist" and prefer_volatile and tier == "stable" and coin_class not in ("large_cap",):
        return False, "watchlist prefers volatile coins"

    return True, ""


def should_include_trending_overlay(cfg: dict | None) -> bool:
    filters = coin_filters_config(cfg or {})
    if not filters.get("allow_trending_watchlist", True):
        return False
    return True


def filter_watchlist_coins(coins: list[dict], cfg: dict) -> list[dict]:
    """Drop watchlist entries that fail profile coin filters."""
    filters = coin_filters_config(cfg)
    if not filters.get("enabled", True):
        return list(coins or [])
    kept: list[dict] = []
    for coin in coins or []:
        ok, _ = passes_coin_filters(coin, None, cfg, context="watchlist")
        if ok:
            kept.append(coin)
    return kept