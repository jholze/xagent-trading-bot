import requests


def get_prices(symbol="ARIA/USDT"):
    """
    Robust multi-coin price fetcher with CoinGecko mapping + Gate.io fallback.
    """
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
                    return price, price, None
    except Exception as e:
        print(f"   [Price] Gate.io failed for {symbol}: {e}")
    print(f"   [Price] Fallback $0.00 for {symbol}")
    return 0.0, 0.0, None

    # Gate.io dynamic fallback
    try:
        pair = symbol.replace("/", "_").upper()
        url = f"https://api.gateio.ws/api/v4/spot/tickers?currency_pair={pair}"
        response = requests.get(url, timeout=8)
        if response.status_code == 200:
            data = response.json()
            if data and len(data) > 0:
                price = float(data[0].get("last", 0))
                if price:
                    print(f"   [Price Fetch] Gate.io → ${price:.4f} for {symbol}")
                    return price, price, None
    except:
        pass

    print(f"   [Price Fetch] No price for {symbol} → Fallback $0.0")
    return 0.0, 0.0, None
