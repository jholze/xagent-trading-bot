"""CMC market-cap lookup with short TTL cache (entry sensor + listings)."""

from __future__ import annotations

import os
import time
from typing import Any

import requests

from logger import log

_BASE_URL = "https://pro-api.coinmarketcap.com/v1"
# value: market cap USD (0.0 = known missing / zero); expires monotonic
_CACHE: dict[str, tuple[float, float]] = {}
_CACHE_TTL_SEC = 3600.0
# Negative cache for HTTP/network failures (avoid 1Hz retry storms)
_FAIL_CACHE: dict[str, float] = {}
_FAIL_TTL_SEC = 900.0


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


def _pick_quote_item(data: dict, base: str) -> dict:
    """CMC may return a dict or list for multi-match symbols (e.g. XAUT)."""
    raw = (data or {}).get(base)
    if isinstance(raw, list):
        return raw[0] if raw else {}
    if isinstance(raw, dict):
        return raw
    # Sometimes keys are cmc ids; scan for matching symbol
    for entry in (data or {}).values():
        if isinstance(entry, list):
            for item in entry:
                if str(item.get("symbol") or "").upper() == base:
                    return item
        elif isinstance(entry, dict) and str(entry.get("symbol") or "").upper() == base:
            return entry
    return {}


def fetch_market_cap_usd(symbol: str, *, api_key: str | None = None) -> float | None:
    base = _base_symbol(symbol)
    if not base:
        return None
    now = time.monotonic()
    cached = _CACHE.get(base)
    if cached and (now - cached[1]) < _CACHE_TTL_SEC:
        mcap = cached[0]
        return mcap if mcap > 0 else None

    fail_at = _FAIL_CACHE.get(base)
    if fail_at is not None and (now - fail_at) < _FAIL_TTL_SEC:
        return None

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
            _FAIL_CACHE[base] = now
            return None
        item = _pick_quote_item(resp.json().get("data") or {}, base)
        quote = (item.get("quote") or {}).get("USD") or {}
        mcap = float(quote.get("market_cap") or 0)
        # Always cache successful HTTP — including mcap=0 — so we never 1Hz-retry
        # symbols like XAUT that return price without usable market_cap.
        _CACHE[base] = (mcap, now)
        _FAIL_CACHE.pop(base, None)
        return mcap if mcap > 0 else None
    except Exception as exc:
        log(f"CMC market cap fetch failed for {base}: {exc}", "WARNING")
        _FAIL_CACHE[base] = now
    return None


def resolve_market_cap_usd(symbol: str, coin: dict | None = None) -> float | None:
    mcap = market_cap_from_coin(coin)
    if mcap is not None:
        return mcap
    return fetch_market_cap_usd(symbol)


def reset_market_cap_cache_for_tests() -> None:
    _CACHE.clear()
    _FAIL_CACHE.clear()


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
