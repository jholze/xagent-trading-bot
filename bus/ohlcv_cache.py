"""OHLCV bar cache — in-memory + optional Redis (PR-P2)."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any

from bus.redis_client import get_redis, resolve_redis_url


@dataclass(frozen=True)
class CachedOhlcvBars:
    symbol: str
    timeframe: str
    limit: int
    bars: list
    exchange: str
    updated_at: float

    @property
    def age_sec(self) -> float:
        return max(0.0, time.time() - self.updated_at)


def _ohlcv_key(key_prefix: str, symbol: str, timeframe: str, limit: int) -> str:
    safe = symbol.replace("/", "_").upper()
    return f"{key_prefix}ohlcv:{safe}:{timeframe}:{limit}"


def _default_ttl_map() -> dict[str, float]:
    return {"15m": 60.0, "1h": 90.0, "4h": 120.0, "30m": 90.0, "2h": 120.0}


def ttl_for_timeframe(timeframe: str, config_raw: dict | None = None) -> float:
    arch = ((config_raw or {}).get("architecture") or {})
    ttl_map = dict(_default_ttl_map())
    custom = arch.get("ohlcv_cache_ttl_sec")
    if isinstance(custom, dict):
        for key, val in custom.items():
            ttl_map[str(key)] = float(val)
    elif custom is not None:
        ttl_map["4h"] = float(custom)
    return float(ttl_map.get(timeframe, ttl_map.get("4h", 120.0)))


class OhlcvCache:
    def __init__(
        self,
        *,
        redis_url: str | None = None,
        key_prefix: str = "aria:",
        config_raw: dict | None = None,
    ):
        self.redis_url = redis_url
        self.key_prefix = key_prefix
        self.config_raw = config_raw
        self._ram: dict[tuple[str, str, int], CachedOhlcvBars] = {}
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0

    def _client(self):
        return get_redis(self.redis_url, key_prefix=self.key_prefix)

    def _cache_key(self, symbol: str, timeframe: str, limit: int) -> tuple[str, str, int]:
        return (symbol, timeframe, int(limit))

    def _is_fresh(self, entry: CachedOhlcvBars, timeframe: str) -> bool:
        return entry.age_sec <= ttl_for_timeframe(timeframe, self.config_raw)

    def get(self, symbol: str, timeframe: str, limit: int) -> CachedOhlcvBars | None:
        key = self._cache_key(symbol, timeframe, limit)
        with self._lock:
            entry = self._ram.get(key)
            if entry and self._is_fresh(entry, timeframe):
                self._hits += 1
                return entry

        client = self._client()
        if client:
            try:
                raw = client.get(_ohlcv_key(self.key_prefix, symbol, timeframe, limit))
                if raw:
                    data = json.loads(raw)
                    entry = CachedOhlcvBars(
                        symbol=symbol,
                        timeframe=timeframe,
                        limit=limit,
                        bars=data.get("bars") or [],
                        exchange=str(data.get("exchange") or "redis"),
                        updated_at=float(data.get("updated_at") or 0),
                    )
                    if entry.bars and self._is_fresh(entry, timeframe):
                        with self._lock:
                            self._ram[key] = entry
                            self._hits += 1
                        return entry
            except (TypeError, ValueError, json.JSONDecodeError):
                pass

        with self._lock:
            self._misses += 1
        return None

    def set(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        bars: list,
        *,
        exchange: str,
    ) -> None:
        if not bars:
            return
        now = time.time()
        entry = CachedOhlcvBars(
            symbol=symbol,
            timeframe=timeframe,
            limit=limit,
            bars=bars,
            exchange=exchange,
            updated_at=now,
        )
        key = self._cache_key(symbol, timeframe, limit)
        with self._lock:
            self._ram[key] = entry

        client = self._client()
        if not client:
            return
        ttl = max(30, int(ttl_for_timeframe(timeframe, self.config_raw)))
        payload = {
            "symbol": symbol,
            "timeframe": timeframe,
            "limit": limit,
            "bars": bars,
            "exchange": exchange,
            "updated_at": now,
        }
        try:
            client.setex(
                _ohlcv_key(self.key_prefix, symbol, timeframe, limit),
                ttl,
                json.dumps(payload, separators=(",", ":")),
            )
        except Exception:
            pass

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self._hits + self._misses
            hit_rate = (self._hits / total * 100.0) if total else 0.0
            return {
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate_pct": round(hit_rate, 1),
                "ram_entries": len(self._ram),
            }

    def reset_stats(self) -> None:
        with self._lock:
            self._hits = 0
            self._misses = 0

    def clear(self) -> None:
        with self._lock:
            self._ram.clear()
            self._hits = 0
            self._misses = 0


_default_cache: OhlcvCache | None = None


def ohlcv_cache_from_config(config_raw: dict | None = None) -> OhlcvCache:
    global _default_cache
    if config_raw is None:
        if _default_cache is not None:
            return _default_cache
        from core.config import get_bot_config

        config_raw = get_bot_config().raw

    arch = (config_raw or {}).get("architecture") or {}
    cache = OhlcvCache(
        redis_url=resolve_redis_url(arch.get("redis_url")),
        key_prefix=str(arch.get("key_prefix", "aria:")),
        config_raw=config_raw,
    )
    if _default_cache is None:
        _default_cache = cache
    return cache


def ohlcv_cache_enabled(config_raw: dict | None = None) -> bool:
    if config_raw is None:
        from core.config import get_bot_config

        config_raw = get_bot_config().raw
    arch = (config_raw or {}).get("architecture") or {}
    return bool(arch.get("ohlcv_cache_enabled", True))


def reset_ohlcv_cache_for_tests() -> None:
    global _default_cache
    _default_cache = None