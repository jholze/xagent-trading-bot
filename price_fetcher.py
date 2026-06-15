import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# In-memory price cache (TTL reduces API spam during Telegram commands)
_price_cache = {}
_last_good_cache = {}
_CACHE_TTL_SECONDS = 30

_CG_MAP = {"ARIA": "aria-ai", "RAVE": "ravedao", "HIGH": "highstreet"}


def _price_decimal_places(value: float, sig_digits: int = 4) -> int:
    """Decimals needed to show sig_digits for micro-cap prices (e.g. CAT @ 0.000001514)."""
    if value <= 0:
        return 9
    exponent = int(math.floor(math.log10(abs(value))))
    if exponent >= -2:
        return 4
    # Minimum 9 decimals for sub-$0.00001 coins (CAT, etc.)
    return min(12, max(9, -exponent + sig_digits - 1))


def format_token_amount(amount: float) -> str:
    """Human-readable token quantity (micro-cap and large lots safe)."""
    value = float(amount or 0)
    if value <= 0:
        return "0"
    if value >= 1000:
        return f"{value:,.4f}"
    if value >= 0.0001:
        return f"{value:.4f}"
    decimals = _price_decimal_places(value)
    return f"{value:.{decimals}f}"


def format_usdt_price(price: float) -> str:
    """Human-readable USDT price (micro-cap safe — avoids $0.0000 for CAT etc.)."""
    value = float(price or 0)
    if value <= 0:
        return "—"
    if value >= 1000:
        return f"${value:,.0f}"
    if value >= 1:
        return f"${value:,.2f}"
    if value >= 0.01:
        return f"${value:.4f}"
    decimals = _price_decimal_places(value)
    # Do not rstrip zeros — 0.000001514 must not collapse to 0.00000151
    return f"${value:.{decimals}f}"


def _format_price_log(price: float) -> str:
    return format_usdt_price(price).replace("$", "")


def _cache_get(symbol: str, now: float = None):
    now = now or time.time()
    if symbol in _price_cache:
        cached_price, cached_time = _price_cache[symbol]
        if now - cached_time < _CACHE_TTL_SECONDS:
            return cached_price
    return None


def _cache_set(symbol: str, price: float, now: float = None):
    now = now or time.time()
    _price_cache[symbol] = (price, now)
    if price > 0:
        _last_good_cache[symbol] = price


def _position_fallbacks(symbols: list[str], fallbacks: dict[str, float] = None) -> dict[str, float]:
    resolved = {}
    for sym in symbols:
        fb = float((fallbacks or {}).get(sym, 0) or 0)
        if fb > 0:
            resolved[sym] = fb
    return resolved


def _apply_price_fallbacks(
    symbols: list[str],
    result: dict[str, float],
    fallbacks: dict[str, float] = None,
) -> dict[str, str]:
    """Fill zero quotes from last good cache, then optional entry-price fallbacks."""
    sources = {}
    fb_map = _position_fallbacks(symbols, fallbacks)
    for sym in symbols:
        price = float(result.get(sym, 0) or 0)
        if price > 0:
            sources[sym] = "live"
            continue
        stale = _last_good_cache.get(sym)
        if stale and stale > 0:
            result[sym] = float(stale)
            sources[sym] = "stale"
            continue
        entry = fb_map.get(sym, 0)
        if entry > 0:
            result[sym] = entry
            sources[sym] = "entry"
            continue
        result[sym] = 0.0
        sources[sym] = "missing"
    return sources


def _fetch_gate_single(symbol: str):
    pair = symbol.replace("/", "_").upper()
    url = f"https://api.gateio.ws/api/v4/spot/tickers?currency_pair={pair}"
    response = requests.get(url, timeout=6)
    if response.status_code == 200:
        data = response.json()
        if data and len(data) > 0:
            price = float(data[0].get("last", 0))
            if price:
                return price
    return None


def _fetch_coingecko_single(cg_id: str):
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={cg_id}&vs_currencies=usd"
    response = requests.get(url, timeout=6)
    if response.status_code == 200:
        data = response.json()
        price = data.get(cg_id, {}).get("usd")
        if price:
            return float(price)
    return None


def _fetch_gate_bulk(symbols: list[str]) -> dict[str, float]:
    """One Gate request for many pairs (faster than N sequential calls)."""
    if not symbols:
        return {}
    pairs_needed = {sym.replace("/", "_").upper() for sym in symbols}
    try:
        response = requests.get(
            "https://api.gateio.ws/api/v4/spot/tickers",
            timeout=12,
        )
        if response.status_code != 200:
            return {}
        found = {}
        for item in response.json():
            pair = item.get("currency_pair", "")
            if pair not in pairs_needed:
                continue
            price = float(item.get("last", 0) or 0)
            if price > 0:
                found[pair.replace("_", "/")] = price
        return found
    except Exception as e:
        print(f"   [Price] Gate bulk failed: {e}")
        return {}


def _fetch_coingecko_bulk(symbols: list[str]) -> dict[str, float]:
    ids = []
    sym_for_id = {}
    for sym in symbols:
        coin = sym.split("/")[0].upper()
        cg_id = _CG_MAP.get(coin)
        if cg_id and cg_id not in sym_for_id:
            ids.append(cg_id)
            sym_for_id[cg_id] = sym
    if not ids:
        return {}
    try:
        url = (
            "https://api.coingecko.com/api/v3/simple/price"
            f"?ids={','.join(ids)}&vs_currencies=usd"
        )
        response = requests.get(url, timeout=8)
        if response.status_code != 200:
            return {}
        data = response.json()
        found = {}
        for cg_id, sym in sym_for_id.items():
            price = data.get(cg_id, {}).get("usd")
            if price:
                found[sym] = float(price)
        return found
    except Exception as e:
        print(f"   [Price] CoinGecko bulk failed: {e}")
        return {}


def _fetch_single_symbol(symbol: str) -> tuple[str, float]:
    """Fetch one symbol using the same source priority as get_prices."""
    coin = symbol.split("/")[0].upper()
    fetch_order = ("gate", "coingecko") if coin not in _CG_MAP else ("coingecko", "gate")
    for source in fetch_order:
        try:
            if source == "gate":
                price = _fetch_gate_single(symbol)
                if price:
                    return symbol, price
            elif coin in _CG_MAP:
                price = _fetch_coingecko_single(_CG_MAP[coin])
                if price:
                    return symbol, price
        except Exception as e:
            print(f"   [Price] {source} failed for {symbol}: {e}")
    return symbol, 0.0


def get_prices_batch(
    symbols: list[str],
    fallbacks: dict[str, float] = None,
    *,
    return_sources: bool = False,
) -> dict[str, float] | tuple[dict[str, float], dict[str, str]]:
    """
    Fetch prices for multiple symbols efficiently.
    Uses cache, then Gate bulk + CoinGecko bulk, then parallel singles.
    Zero quotes fall back to last good cache, then optional entry prices.
    """
    if not symbols:
        return ({}, {}) if return_sources else {}

    unique = list(dict.fromkeys(symbols))
    now = time.time()
    result = {}
    missing = []

    for sym in unique:
        cached = _cache_get(sym, now)
        if cached is not None:
            result[sym] = cached
        else:
            missing.append(sym)

    if not missing:
        for sym in unique:
            result.setdefault(sym, 0.0)
        sources = _apply_price_fallbacks(unique, result, fallbacks)
        if return_sources:
            return result, sources
        return result

    gate_hits = _fetch_gate_bulk(missing)
    for sym, price in gate_hits.items():
        result[sym] = price
        _cache_set(sym, price, now)
        print(f"   [Price] Gate.io (bulk) → {format_usdt_price(price)} | {sym}")

    missing = [sym for sym in missing if sym not in result]

    if missing:
        cg_hits = _fetch_coingecko_bulk(missing)
        for sym, price in cg_hits.items():
            result[sym] = price
            _cache_set(sym, price, now)
            print(f"   [Price] CoinGecko (bulk) → {format_usdt_price(price)} | {sym}")
        missing = [sym for sym in missing if sym not in result]

    if missing:
        workers = min(8, len(missing))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_fetch_single_symbol, sym): sym for sym in missing}
            for future in as_completed(futures):
                sym, price = future.result()
                result[sym] = price
                if price > 0:
                    _cache_set(sym, price, now)
                    print(f"   [Price] parallel → {format_usdt_price(price)} | {sym}")

    for sym in unique:
        result.setdefault(sym, 0.0)

    sources = _apply_price_fallbacks(unique, result, fallbacks)
    if return_sources:
        return result, sources
    return result


def get_prices(symbol="ARIA/USDT"):
    """
    Robust multi-coin price fetcher with CoinGecko mapping + Gate.io fallback.
    Includes a small cache to avoid hammering external APIs on repeated calls.
    """
    batch = get_prices_batch([symbol])
    price = batch.get(symbol, 0.0)
    return price, price, None