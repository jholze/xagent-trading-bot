import time
import requests

# Simple in-memory price cache (short TTL to reduce API spam during Telegram commands)
_price_cache = {}
_CACHE_TTL_SECONDS = 12


def get_prices(symbol="ARIA/USDT"):
    """
    Robust multi-coin price fetcher with CoinGecko mapping + Gate.io fallback.
    Includes a small cache to avoid hammering external APIs on repeated calls.
    """
    now = time.time()

    # Return cached price if still fresh
    if symbol in _price_cache:
        cached_price, cached_time = _price_cache[symbol]
        if now - cached_time < _CACHE_TTL_SECONDS:
            return cached_price, cached_price, None

    coin = symbol.split("/")[0].upper()
    cg_map = {"ARIA": "aria-ai", "RAVE": "ravedao", "HIGH": "highstreet", "DEFAULT": "aria-ai"}
    cg_id = cg_map.get(coin, cg_map["DEFAULT"])

    try:
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={cg_id}&vs_currencies=usd"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            price = data.get(cg_id, {}).get("usd")
            if price:
                print(f"   [Price] CoinGecko → ${price:.4f} | {symbol}")
                _price_cache[symbol] = (price, now)
                return price, price, None
    except Exception as e:
        print(f"   [Price] CoinGecko failed for {symbol}: {e}")

    try:
        pair = symbol.replace("/", "_").upper()
        url = f"https://api.gateio.ws/api/v4/spot/tickers?currency_pair={pair}"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data and len(data) > 0:
                price = float(data[0].get("last", 0))
                if price:
                    print(f"   [Price] Gate.io → ${price:.4f} | {symbol}")
                    _price_cache[symbol] = (price, now)
                    return price, price, None
    except Exception as e:
        print(f"   [Price] Gate.io failed for {symbol}: {e}")

    print(f"   [Price] Fallback $0.00 for {symbol}")
    return 0.0, 0.0, None
