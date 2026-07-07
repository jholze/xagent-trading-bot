"""Coin price queries — Redis-first with exchange fallback."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from bus.price_cache import CachedPrice, price_cache_enabled, price_cache_from_config
from price_fetcher import get_prices_batch


@dataclass
class CoinPriceResult:
    symbol: str
    price: float
    source: str
    age_sec: float | None = None


@dataclass
class CoinQueryResponse:
    prices: dict[str, CoinPriceResult] = field(default_factory=dict)
    redis_available: bool = False
    cache_hits: int = 0
    fetched: int = 0


def normalize_symbols(raw: list[str] | str | None) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.replace(";", ",").split(",")]
    else:
        parts = [str(p).strip() for p in raw]
    out: list[str] = []
    for part in parts:
        if not part:
            continue
        sym = part.upper()
        if "/" not in sym:
            sym = f"{sym}/USDT"
        out.append(sym)
    return list(dict.fromkeys(out))


def webhook_token_ok(provided: str | None, config_raw: dict | None = None) -> bool:
    env_token = os.environ.get("COIN_WEBHOOK_TOKEN", "").strip()
    if env_token:
        return (provided or "").strip() == env_token
    if config_raw is None:
        from core.config import get_bot_config

        config_raw = get_bot_config().raw
    arch = (config_raw or {}).get("architecture") or {}
    cfg_token = str(arch.get("coin_query_webhook_token") or "").strip()
    if cfg_token:
        return (provided or "").strip() == cfg_token
    return True


def query_coin_prices(
    symbols: list[str],
    *,
    fallbacks: dict[str, float] | None = None,
    config_raw: dict | None = None,
    force_refresh: bool = False,
) -> CoinQueryResponse:
    unique = normalize_symbols(symbols)
    response = CoinQueryResponse()
    if not unique:
        return response

    cache = price_cache_from_config(config_raw)
    response.redis_available = cache.available()
    cached: dict[str, CachedPrice] = {}
    missing = list(unique)

    if price_cache_enabled(config_raw) and response.redis_available and not force_refresh:
        cached = cache.get_many(unique)
        response.cache_hits = len(cached)
        missing = [sym for sym in unique if sym not in cached]

    fetched_prices: dict[str, float] = {}
    fetched_sources: dict[str, str] = {}
    if missing:
        batch = get_prices_batch(missing, fallbacks=fallbacks, return_sources=True)
        if isinstance(batch, tuple):
            fetched_prices, fetched_sources = batch
        else:
            fetched_prices = batch
        response.fetched = len(missing)

    for sym in unique:
        if sym in cached:
            cp = cached[sym]
            response.prices[sym] = CoinPriceResult(
                symbol=sym,
                price=cp.price,
                source="redis",
                age_sec=round(cp.age_sec, 1),
            )
            continue
        price = float(fetched_prices.get(sym, 0) or 0)
        src = fetched_sources.get(sym, "live")
        response.prices[sym] = CoinPriceResult(
            symbol=sym,
            price=price,
            source=src,
            age_sec=0.0 if price > 0 else None,
        )

    if (
        price_cache_enabled(config_raw)
        and response.redis_available
        and fetched_prices
    ):
        to_store = {sym: float(fetched_prices.get(sym, 0) or 0) for sym in missing}
        to_store = {sym: val for sym, val in to_store.items() if val > 0}
        if to_store:
            cache.set_many(to_store, sources=fetched_sources)

    return response


def response_to_dict(result: CoinQueryResponse) -> dict:
    return {
        "redis_available": result.redis_available,
        "cache_hits": result.cache_hits,
        "fetched": result.fetched,
        "prices": {
            sym: {
                "price": item.price,
                "source": item.source,
                "age_sec": item.age_sec,
            }
            for sym, item in result.prices.items()
        },
    }