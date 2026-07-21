"""Shared Redis price cache — fast coin lookups for /positions and webhooks."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from bus.redis_client import get_redis, resolve_redis_url


@dataclass(frozen=True)
class CachedPrice:
    symbol: str
    price: float
    source: str
    updated_at: float

    @property
    def age_sec(self) -> float:
        return max(0.0, time.time() - self.updated_at)


def _price_key(key_prefix: str, symbol: str) -> str:
    safe = symbol.replace("/", "_").upper()
    return f"{key_prefix}price:{safe}"


def _meta_key(key_prefix: str) -> str:
    return f"{key_prefix}price:meta:last_refresh"


class RedisPriceCache:
    def __init__(
        self,
        *,
        redis_url: str | None = None,
        key_prefix: str = "aria:",
        ttl_sec: float = 120.0,
    ):
        self.redis_url = redis_url
        self.key_prefix = key_prefix
        self.ttl_sec = float(ttl_sec)

    def _client(self):
        return get_redis(self.redis_url, key_prefix=self.key_prefix)

    def available(self) -> bool:
        client = self._client()
        if not client:
            return False
        try:
            return bool(client.ping())
        except Exception:
            return False

    def get_many(self, symbols: list[str]) -> dict[str, CachedPrice]:
        client = self._client()
        if not client or not symbols:
            return {}
        unique = list(dict.fromkeys(symbols))
        keys = [_price_key(self.key_prefix, sym) for sym in unique]
        try:
            raw_values = client.mget(keys)
        except Exception:
            return {}
        found: dict[str, CachedPrice] = {}
        for sym, raw in zip(unique, raw_values):
            if not raw:
                continue
            try:
                data = json.loads(raw)
                price = float(data.get("price", 0) or 0)
                if price <= 0:
                    continue
                found[sym] = CachedPrice(
                    symbol=sym,
                    price=price,
                    source=str(data.get("source") or "redis"),
                    updated_at=float(data.get("updated_at") or 0),
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return found

    def set_many(
        self,
        prices: dict[str, float],
        *,
        sources: dict[str, str] | None = None,
    ) -> int:
        client = self._client()
        if not client or not prices:
            return 0
        now = time.time()
        ttl = max(30, int(self.ttl_sec))
        written = 0
        pipe = client.pipeline()
        for sym, price in prices.items():
            val = float(price or 0)
            if val <= 0:
                continue
            payload = {
                "symbol": sym,
                "price": val,
                "source": (sources or {}).get(sym, "live"),
                "updated_at": now,
            }
            key = _price_key(self.key_prefix, sym)
            pipe.setex(key, ttl, json.dumps(payload, separators=(",", ":")))
            written += 1
        if written:
            pipe.setex(
                _meta_key(self.key_prefix),
                ttl,
                json.dumps({"updated_at": now, "count": written}, separators=(",", ":")),
            )
            try:
                pipe.execute()
            except Exception:
                return 0
        return written

    def last_refresh(self) -> dict[str, Any] | None:
        client = self._client()
        if not client:
            return None
        try:
            raw = client.get(_meta_key(self.key_prefix))
            if not raw:
                return None
            return json.loads(raw)
        except Exception:
            return None


_default_cache: RedisPriceCache | None = None


def price_cache_from_config(config_raw: dict | None = None) -> RedisPriceCache:
    global _default_cache
    if config_raw is None:
        if _default_cache is not None:
            return _default_cache
        from core.config import get_bot_config

        config_raw = get_bot_config().raw

    arch = (config_raw or {}).get("architecture") or {}
    ttl = float(arch.get("price_cache_ttl_sec", 120))
    cache = RedisPriceCache(
        redis_url=resolve_redis_url(arch.get("redis_url")),
        key_prefix=str(arch.get("key_prefix", "aria:")),
        ttl_sec=ttl,
    )
    if _default_cache is None:
        _default_cache = cache
    return cache


def price_cache_enabled(config_raw: dict | None = None) -> bool:
    if config_raw is None:
        from core.config import get_bot_config

        config_raw = get_bot_config().raw
    arch = (config_raw or {}).get("architecture") or {}
    return bool(arch.get("price_cache_enabled", True))


def clear_redis_price_cache(cache: RedisPriceCache | None = None) -> int:
    """Best-effort delete of Redis price keys for this bot prefix. Returns delete count."""
    cache = cache or price_cache_from_config()
    client = cache._client()
    if not client:
        return 0
    pattern = f"{cache.key_prefix}price:*"
    deleted = 0
    try:
        keys = list(client.scan_iter(match=pattern, count=200))
        for i in range(0, len(keys), 200):
            batch = keys[i : i + 200]
            if batch:
                deleted += int(client.delete(*batch))
    except Exception:
        return deleted
    return deleted


def reset_price_cache_for_tests() -> None:
    global _default_cache
    _default_cache = None