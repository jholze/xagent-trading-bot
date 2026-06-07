import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# In-memory price cache (TTL reduces API spam during Telegram commands)
_price_cache = {}
_CACHE_TTL_SECONDS = 30

_CG_MAP = {"ARIA": "aria-ai", "RAVE": "ravedao", "HIGH": "highstreet"}


def _cache_get(symbol: str, now: float = None):
    now = now or time.time()
    if symbol in _price_cache:
        cached_price, cached_time = _price_cache[symbol]
        if now - cached_time < _CACHE_TTL_SECONDS:
            return cached_price
    return None


def _cache_set(symbol: str, price: float, now: float = None):
    _price_cache[symbol] = (price, now or time.time())


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


def get_prices_batch(symbols: list[str]) -> dict[str, float]:
    """
    Fetch prices for multiple symbols efficiently.
    Uses cache, then Gate bulk + CoinGecko bulk, then parallel singles.
    """
    if not symbols:
        return {}

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
        return result

    gate_hits = _fetch_gate_bulk(missing)
    for sym, price in gate_hits.items():
        result[sym] = price
        _cache_set(sym, price, now)
        print(f"   [Price] Gate.io (bulk) → ${price:.4f} | {sym}")

    missing = [sym for sym in missing if sym not in result]

    if missing:
        cg_hits = _fetch_coingecko_bulk(missing)
        for sym, price in cg_hits.items():
            result[sym] = price
            _cache_set(sym, price, now)
            print(f"   [Price] CoinGecko (bulk) → ${price:.4f} | {sym}")
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
                    print(f"   [Price] parallel → ${price:.4f} | {sym}")

    for sym in unique:
        result.setdefault(sym, 0.0)

    return result


def get_prices(symbol="ARIA/USDT"):
    """
    Robust multi-coin price fetcher with CoinGecko mapping + Gate.io fallback.
    Includes a small cache to avoid hammering external APIs on repeated calls.
    """
    batch = get_prices_batch([symbol])
    price = batch.get(symbol, 0.0)
    return price, price, None