"""CMC market-cap lookup with short TTL cache (entry sensor + listings)."""

from __future__ import annotations

import os
import time
from typing import Any

import requests

from logger import log

_BASE_URL = "https://pro-api.coinmarketcap.com/v1"
_CACHE: dict[str, tuple[float, float]] = {}
_CACHE_TTL_SEC = 3600.0


def _base_symbol(symbol: str) -> str:
    return (symbol or "").split("/")[0].upper()


def market_cap_from_coin(coin: dict | None) -> float | None:
    if not coin:
        return None
    for key in ("market_cap_usd", "market_cap"):
        raw = coin.get(key)
        if raw is not None:
            val = float(raw)
            if val > 0:
                return val
    return None


def fetch_market_cap_usd(symbol: str, *, api_key: str | None = None) -> float | None:
    base = _base_symbol(symbol)
    if not base:
        return None
    now = time.monotonic()
    cached = _CACHE.get(base)
    if cached and (now - cached[1]) < _CACHE_TTL_SEC:
        return cached[0]

    key = (api_key or os.getenv("CMC_API_KEY") or "").strip()
    if not key:
        return None
    try:
        resp = requests.get(
            f"{_BASE_URL}/cryptocurrency/quotes/latest",
            headers={"X-CMC_PRO_API_KEY": key, "Accept": "application/json"},
            params={"symbol": base, "convert": "USD"},
            timeout=12,
        )
        if resp.status_code != 200:
            return None
        item = (resp.json().get("data") or {}).get(base) or {}
        quote = (item.get("quote") or {}).get("USD") or {}
        mcap = float(quote.get("market_cap") or 0)
        if mcap > 0:
            _CACHE[base] = (mcap, now)
            return mcap
    except Exception as exc:
        log(f"CMC market cap fetch failed for {base}: {exc}", "WARNING")
    return None


def resolve_market_cap_usd(symbol: str, coin: dict | None = None) -> float | None:
    mcap = market_cap_from_coin(coin)
    if mcap is not None:
        return mcap
    return fetch_market_cap_usd(symbol)


def reset_market_cap_cache_for_tests() -> None:
    _CACHE.clear()


def entry_sensor_mcap_bounds(cfg: dict) -> tuple[float, float | None]:
    min_usd = float(cfg.get("market_cap_min_usd", 5_000_000))
    raw_max = cfg.get("market_cap_max_usd")
    if raw_max is None:
        return min_usd, None
    max_usd = float(raw_max)
    return min_usd, None if max_usd <= 0 else max_usd


def passes_market_cap_filter(
    mcap_usd: float | None,
    cfg: dict,
    *,
    require_known: bool = False,
) -> tuple[bool, str]:
    min_usd, max_usd = entry_sensor_mcap_bounds(cfg)
    if mcap_usd is None or mcap_usd <= 0:
        if require_known:
            return False, "market cap unknown"
        return True, ""
    if mcap_usd < min_usd:
        return False, f"market cap ${mcap_usd / 1e6:.1f}M < ${min_usd / 1e6:.1f}M min"
    if max_usd is not None and mcap_usd > max_usd:
        return False, f"market cap ${mcap_usd / 1e6:.1f}M > ${max_usd / 1e6:.1f}M max"
    return True, ""