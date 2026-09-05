"""OHLCV bar cache — in-memory + optional Redis (PR-P2)."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

from bus.redis_client import get_redis, resolve_redis_url

_DEFAULT_KEY_PREFIX = "aria:"
_ENV_KEY_PREFIX = "OHLCV_CACHE_KEY_PREFIX"


def _env_key_prefix() -> str | None:
    raw = (os.environ.get(_ENV_KEY_PREFIX) or "").strip()
    return raw or None


@dataclass(frozen=True)
class CachedOhlcvBars:
    symbol: str
    timeframe: str
    limit: int
    bars: list
    exchange: str
    updated_at: float
    fetched_at: float | None = None

    @property
    def age_sec(self) -> float:
        stamp = self.fetched_at if self.fetched_at is not None else self.updated_at
        return max(0.0, time.time() - stamp)


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
        key_prefix: str | None = None,
        config_raw: dict | None = None,
    ):
        self.redis_url = redis_url
        self.key_prefix = key_prefix or _env_key_prefix() or _DEFAULT_KEY_PREFIX
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
        if entry.fetched_at is None:
            return False
        return entry.age_sec <= ttl_for_timeframe(timeframe, self.config_raw)

    def _serve_from_larger_enabled(self) -> bool:
        arch = (self.config_raw or {}).get("architecture") or {}
        return bool(arch.get("ohlcv_serve_from_larger", True))

    @staticmethod
    def _slice_entry(entry: CachedOhlcvBars, limit: int) -> CachedOhlcvBars:
        """Return a view with at most `limit` trailing bars (same series, smaller request)."""
        limit = int(limit)
        bars = entry.bars or []
        if len(bars) > limit:
            bars = bars[-limit:]
        return CachedOhlcvBars(
            symbol=entry.symbol,
            timeframe=entry.timeframe,
            limit=limit,
            bars=bars,
            exchange=entry.exchange,
            updated_at=entry.updated_at,
            fetched_at=entry.fetched_at,
        )

    def _load_redis_entry(
        self, symbol: str, timeframe: str, limit: int
    ) -> CachedOhlcvBars | None:
        client = self._client()
        if not client:
            return None
        try:
            raw = client.get(_ohlcv_key(self.key_prefix, symbol, timeframe, limit))
            if not raw:
                return None
            data = json.loads(raw)
            fetched_raw = data.get("fetched_at")
            fetched_at = float(fetched_raw) if fetched_raw is not None else None
            entry = CachedOhlcvBars(
                symbol=symbol,
                timeframe=timeframe,
                limit=int(data.get("limit") or limit),
                bars=data.get("bars") or [],
                exchange=str(data.get("exchange") or "redis"),
                updated_at=float(data.get("updated_at") or fetched_at or 0),
                fetched_at=fetched_at,
            )
            if entry.bars and self._is_fresh(entry, timeframe):
                return entry
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return None

    def get(self, symbol: str, timeframe: str, limit: int) -> CachedOhlcvBars | None:
        limit = int(limit)
        key = self._cache_key(symbol, timeframe, limit)

        with self._lock:
            entry = self._ram.get(key)
            if entry and self._is_fresh(entry, timeframe):
                self._hits += 1
                return entry

        redis_exact = self._load_redis_entry(symbol, timeframe, limit)
        if redis_exact is not None:
            with self._lock:
                self._ram[key] = redis_exact
                self._hits += 1
            return redis_exact

        if self._serve_from_larger_enabled():
            # RAM: any fresh entry for same symbol+tf with enough bars
            with self._lock:
                candidates = [
                    e
                    for (s, tf, _L), e in self._ram.items()
                    if s == symbol
                    and tf == timeframe
                    and e
                    and int(e.limit) >= limit
                    and len(e.bars or []) >= limit
                    and self._is_fresh(e, timeframe)
                ]
                if candidates:
                    # Prefer smallest sufficient store (less over-fetch noise)
                    best = min(candidates, key=lambda e: int(e.limit))
                    sliced = self._slice_entry(best, limit)
                    self._ram[key] = sliced
                    self._hits += 1
                    return sliced

            # Redis: try common larger limits used by the bot (regime=300, indicators=100, …)
            for larger in (300, 250, 200, 150, 120, 100, 80, 60, 50):
                if larger < limit:
                    continue
                found = self._load_redis_entry(symbol, timeframe, larger)
                if found is None or len(found.bars or []) < limit:
                    continue
                sliced = self._slice_entry(found, limit)
                with self._lock:
                    self._ram[key] = sliced
                    self._hits += 1
                return sliced

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
            fetched_at=now,
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
            "fetched_at": now,
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
        key_prefix=_env_key_prefix() or str(arch.get("key_prefix", _DEFAULT_KEY_PREFIX)),
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


def _delete_redis_ohlcv_keys(key_prefix: str, redis_url: str | None = None) -> None:
    """SCAN + DEL `{key_prefix}ohlcv:*`. Swallows all errors (tests without Redis)."""
    try:
        client = get_redis(redis_url, key_prefix=key_prefix)
        if not client:
            return
        pattern = f"{key_prefix}ohlcv:*"
        keys = list(client.scan_iter(match=pattern, count=200))
        for i in range(0, len(keys), 200):
            batch = keys[i : i + 200]
            if batch:
                client.delete(*batch)
    except Exception:
        return


def reset_ohlcv_cache_for_tests() -> None:
    global _default_cache
    prefix = _DEFAULT_KEY_PREFIX
    redis_url = None
    if _default_cache is not None:
        prefix = _default_cache.key_prefix
        redis_url = _default_cache.redis_url
    else:
        prefix = _env_key_prefix() or _DEFAULT_KEY_PREFIX
    _delete_redis_ohlcv_keys(prefix, redis_url)
    _default_cache = None