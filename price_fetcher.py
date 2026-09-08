import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from core.config import get_bot_config
from logger import log

# In-memory price cache (TTL reduces API spam during Telegram commands)
_price_cache = {}
# Last successful quote: {symbol: (price, monotonic_ts)}. Bare floats = age 0.
_last_good_cache = {}
_stale_warned: set[str] = set()
_CACHE_TTL_SECONDS = 30
_STALE_PRICE_MAX_AGE_DEFAULT = 300.0
_GATE_TICKERS_URL = "https://api.gateio.ws/api/v4/spot/tickers"
_GATE_SNAPSHOT_TTL_DEFAULT = 25.0
_GATE_SNAPSHOT_STALE_MULT = 5.0

# Process-wide Gate /spot/tickers snapshot. Injectable clock for tests.
_now = time.monotonic
_gate_snapshot_lock = threading.Lock()
_gate_snapshot: dict[str, float] | None = None
_gate_snapshot_ts: float | None = None


def clear_price_cache() -> int:
    """Drop in-memory price TTL cache (soft hot-reload / tests). Returns entries cleared."""
    n = len(_price_cache)
    _price_cache.clear()
    # Keep _last_good_cache as emergency fallback for missing API; only drop TTL layer.
    # Expiry is still applied on read.
    return n


def reset_gate_ticker_snapshot_for_tests() -> None:
    """Drop the process-wide Gate ticker snapshot (tests)."""
    global _gate_snapshot, _gate_snapshot_ts
    with _gate_snapshot_lock:
        _gate_snapshot = None
        _gate_snapshot_ts = None


def peek_cached_price(symbol: str) -> float | None:
    """Last known price from TTL cache, last-good, or the in-memory Gate snapshot.

    Does not start a network fetch. Missing / invalid → None.
    """
    try:
        key = _slash_pair(symbol)
        now = time.time()
        for cand in (key, str(symbol or "")):
            if not cand:
                continue
            cached = _cache_get(cand, now)
            if cached is not None:
                try:
                    price = float(cached)
                except (TypeError, ValueError):
                    price = 0.0
                if price > 0:
                    return price
        snap = _gate_snapshot
        if snap:
            raw = snap.get(key)
            if raw is None and key != symbol:
                raw = snap.get(symbol)
            try:
                price = float(raw or 0)
            except (TypeError, ValueError):
                price = 0.0
            if price > 0:
                return price
        now_mono = time.monotonic()
        parsed = _parse_last_good(
            _last_good_cache.get(key) or _last_good_cache.get(symbol),
            now_mono=now_mono,
        )
        if parsed is not None:
            price, ts = parsed
            if price > 0 and (now_mono - ts) <= _stale_price_max_age_sec():
                return float(price)
    except Exception:
        return None
    return None


def _stale_price_max_age_sec() -> float:
    try:
        return float(get_bot_config().stale_price_max_age_sec)
    except Exception:
        return _STALE_PRICE_MAX_AGE_DEFAULT


def _parse_last_good(entry, *, now_mono: float) -> tuple[float, float] | None:
    """Return (price, monotonic_ts) or None. Untimestamped floats count as age 0."""
    if entry is None:
        return None
    if isinstance(entry, (tuple, list)) and len(entry) >= 2:
        try:
            price = float(entry[0] or 0)
            ts = float(entry[1])
        except (TypeError, ValueError):
            return None
        if price > 0:
            return price, ts
        return None
    try:
        price = float(entry or 0)
    except (TypeError, ValueError):
        return None
    if price > 0:
        return price, now_mono
    return None


def stale_expired_symbols() -> set[str]:
    """Symbols whose last-good quote is older than architecture.stale_price_max_age_sec."""
    now_mono = time.monotonic()
    max_age = _stale_price_max_age_sec()
    expired: set[str] = set()
    for sym, entry in _last_good_cache.items():
        parsed = _parse_last_good(entry, now_mono=now_mono)
        if parsed is None:
            continue
        _price, ts = parsed
        if (now_mono - ts) > max_age:
            expired.add(sym)
    return expired

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
        _last_good_cache[symbol] = (price, time.monotonic())
        _stale_warned.discard(symbol)


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
    *,
    allow_entry_price_fallback: bool = False,
) -> dict[str, str]:
    """Fill zero quotes from last-good cache (TTL), then optional entry-price fallbacks.

    Entry-price substitution is display-only: trading callers must leave
    ``allow_entry_price_fallback`` at its default False.
    """
    sources = {}
    fb_map = _position_fallbacks(symbols, fallbacks) if allow_entry_price_fallback else {}
    now_mono = time.monotonic()
    max_age = _stale_price_max_age_sec()
    for sym in symbols:
        price = float(result.get(sym, 0) or 0)
        if price > 0:
            sources[sym] = "live"
            continue
        parsed = _parse_last_good(_last_good_cache.get(sym), now_mono=now_mono)
        if parsed is not None:
            stale_price, ts = parsed
            age = now_mono - ts
            if age <= max_age:
                result[sym] = stale_price
                sources[sym] = "stale"
                continue
            result[sym] = 0.0
            sources[sym] = "stale_expired"
            if sym not in _stale_warned:
                log(
                    f"Stale price expired for {sym}: age {age:.0f}s > {max_age:.0f}s "
                    "— serving 0.0 (stale_expired)",
                    "WARNING",
                )
                _stale_warned.add(sym)
            continue
        entry = fb_map.get(sym, 0)
        if allow_entry_price_fallback and entry > 0:
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


def _gate_ticker_snapshot_ttl_sec() -> float:
    try:
        return float(get_bot_config().gate_ticker_snapshot_ttl_sec)
    except Exception:
        return _GATE_SNAPSHOT_TTL_DEFAULT


def _slash_pair(symbol: str) -> str:
    return str(symbol or "").replace("/", "_").upper().replace("_", "/")


def _download_gate_ticker_snapshot() -> dict[str, float]:
    """HTTP GET /spot/tickers → {SYM/USDT: last}. Logs one INFO line per download."""
    t0 = _now()
    response = requests.get(_GATE_TICKERS_URL, timeout=12)
    elapsed_ms = (_now() - t0) * 1000.0
    try:
        size = len(response.content or b"")
    except TypeError:
        size = 0
    log(f"Gate ticker snapshot: {size} bytes in {elapsed_ms:.0f}ms", "INFO")
    if getattr(response, "status_code", None) != 200:
        raise RuntimeError(
            f"gate tickers HTTP {getattr(response, 'status_code', None)}"
        )
    found: dict[str, float] = {}
    for item in response.json():
        pair = item.get("currency_pair", "")
        if not pair:
            continue
        try:
            price = float(item.get("last", 0) or 0)
        except (TypeError, ValueError):
            continue
        if price > 0:
            found[_slash_pair(pair)] = price
    return found


def _gate_ticker_snapshot() -> dict[str, float]:
    """Whole-market last prices, refreshed at most every gate_ticker_snapshot_ttl_sec.

    Concurrent callers share one in-flight download (module lock). A failed
    download returns the previous snapshot if it is younger than 5× TTL;
    otherwise the previous error result (empty dict).
    """
    global _gate_snapshot, _gate_snapshot_ts
    ttl = _gate_ticker_snapshot_ttl_sec()
    now = _now()
    snap = _gate_snapshot
    ts = _gate_snapshot_ts
    if snap is not None and ts is not None and (now - ts) < ttl:
        return snap
    with _gate_snapshot_lock:
        now = _now()
        snap = _gate_snapshot
        ts = _gate_snapshot_ts
        if snap is not None and ts is not None and (now - ts) < ttl:
            return snap
        try:
            downloaded = _download_gate_ticker_snapshot()
        except Exception as e:
            print(f"   [Price] Gate bulk failed: {e}")
            if (
                snap is not None
                and ts is not None
                and (now - ts) < (ttl * _GATE_SNAPSHOT_STALE_MULT)
            ):
                return snap
            return {}
        _gate_snapshot = downloaded
        _gate_snapshot_ts = _now()
        return downloaded


def _fetch_gate_bulk(symbols: list[str]) -> dict[str, float]:
    """Requested pairs from the process-wide Gate ticker snapshot.

    Signature and return contract are unchanged: {SYM/USDT: price} for hits
    with price > 0; empty dict on no symbols / failed download without stale.
    """
    if not symbols:
        return {}
    snapshot = _gate_ticker_snapshot()
    if not snapshot:
        return {}
    found = {}
    for sym in symbols:
        key = _slash_pair(sym)
        try:
            price = float(snapshot.get(key, 0) or 0)
        except (TypeError, ValueError):
            continue
        if price > 0:
            found[key] = price
    return found


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
    allow_entry_price_fallback: bool = False,
) -> dict[str, float] | tuple[dict[str, float], dict[str, str]]:
    """
    Fetch prices for multiple symbols efficiently.
    Uses cache, then Gate bulk + CoinGecko bulk, then parallel singles.
    Zero quotes fall back to last-good cache (capped by stale_price_max_age_sec).
    Entry-price fallback is opt-in and must not be used for trading decisions.
    """
    if not symbols:
        return ({}, {}) if return_sources else {}

    unique = list(dict.fromkeys(symbols))
    now = time.time()
    result = {}
    missing = []
    redis_sources: dict[str, str] = {}

    for sym in unique:
        cached = _cache_get(sym, now)
        if cached is not None:
            result[sym] = cached
        else:
            missing.append(sym)

    if missing:
        try:
            from bus.price_cache import price_cache_enabled, price_cache_from_config

            if price_cache_enabled():
                cache = price_cache_from_config()
                if cache.available():
                    for sym, entry in cache.get_many(missing).items():
                        result[sym] = entry.price
                        _cache_set(sym, entry.price, now)
                        redis_sources[sym] = "redis"
                    missing = [sym for sym in missing if sym not in result]
        except Exception:
            pass

    if not missing:
        for sym in unique:
            result.setdefault(sym, 0.0)
        sources = _apply_price_fallbacks(
            unique, result, fallbacks,
            allow_entry_price_fallback=allow_entry_price_fallback,
        )
        sources.update(redis_sources)
        if return_sources:
            return result, sources
        return result

    network_missing = list(missing)
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

    try:
        from bus.price_cache import price_cache_enabled, price_cache_from_config

        if price_cache_enabled() and network_missing:
            cache = price_cache_from_config()
            if cache.available():
                live_sources = {}
                to_store = {}
                for sym in network_missing:
                    price = float(result.get(sym, 0) or 0)
                    if price > 0:
                        to_store[sym] = price
                        live_sources[sym] = "live"
                if to_store:
                    cache.set_many(to_store, sources=live_sources)
    except Exception:
        pass

    sources = _apply_price_fallbacks(
        unique, result, fallbacks,
        allow_entry_price_fallback=allow_entry_price_fallback,
    )
    sources.update(redis_sources)
    if return_sources:
        return result, sources
    return result


def get_gate_prices_batch(symbols: list[str]) -> dict[str, float]:
    """Gate.io spot prices only — no CoinGecko, cache, or entry fallbacks."""
    if not symbols:
        return {}
    unique = list(dict.fromkeys(symbols))
    result = {sym: 0.0 for sym in unique}
    for sym, price in _fetch_gate_bulk(unique).items():
        result[sym] = float(price)
    for sym in unique:
        if result[sym] > 0:
            continue
        price = _fetch_gate_single(sym)
        if price:
            result[sym] = float(price)
    return result


def is_gate_tradeable(symbol: str, *, gate_price: float | None = None) -> bool:
    if gate_price is not None:
        return float(gate_price or 0) > 0
    return float(get_gate_prices_batch([symbol]).get(symbol, 0) or 0) > 0


def get_ticker_price(symbol: str, exchange: str | None = None) -> float:
    """Fetch ticker price specifically for the given (or configured primary) exchange.

    This is the source of truth for "is this coin listed and has price on our exchange?"
    Falls back to general batch only if specific fetch fails.
    """
    from core.config import get_bot_config
    ex = (exchange or get_bot_config().exchange or "gate").lower()

    # Fast path for known exchanges with bulk support
    if ex == "gate":
        return float(get_gate_prices_batch([symbol]).get(symbol, 0) or 0)

    # For other exchanges, try direct CCXT ticker on the specific exchange.
    try:
        from services.market_service import MarketService
        ms = MarketService()
        ex_obj = ms._get_spot_exchange(ex)
        ticker = ex_obj.fetch_ticker(symbol)
        last = ticker.get("last") or ticker.get("close") or 0
        return float(last or 0)
    except Exception:
        # Last resort fallback
        return float(get_prices_batch([symbol]).get(symbol, 0) or 0)


def is_listed_on_exchange(symbol: str, exchange: str | None = None) -> bool:
    """True if we can get a positive price from the configured (or given) exchange."""
    return get_ticker_price(symbol, exchange) > 0


def _exchange_label(exchange: str) -> str:
    labels = {"gate": "Gate.io", "binance": "Binance"}
    return labels.get((exchange or "").lower(), exchange or "exchange")


def passes_exchange_filter(
    symbol: str,
    cfg: dict,
    *,
    exchange: str | None = None,
    price: float | None = None,
) -> tuple[bool, str]:
    """Generic filter: only allow coins that are actually listed on our configured exchange."""
    if not cfg.get("exchange_only", cfg.get("gate_only", True)):
        return True, ""
    ex = exchange or get_bot_config().exchange
    if price is not None:
        listed = float(price or 0) > 0
    else:
        listed = is_listed_on_exchange(symbol, ex)
    if listed:
        return True, ""
    return False, f"not listed on {_exchange_label(ex)}"


# Legacy support
def passes_gate_filter(symbol: str, cfg: dict, *, gate_price: float | None = None) -> tuple[bool, str]:
    return passes_exchange_filter(symbol, cfg, exchange="gate", price=gate_price)


def get_prices(symbol="ARIA/USDT"):
    """
    Robust multi-coin price fetcher with CoinGecko mapping + Gate.io fallback.
    Includes a small cache to avoid hammering external APIs on repeated calls.
    """
    batch = get_prices_batch([symbol])
    price = batch.get(symbol, 0.0)
    return price, price, None