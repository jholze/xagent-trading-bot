from datetime import datetime, timedelta, timezone

import ccxt
import pandas as pd
import talib

from logger import log

_ohlcv_cache: dict[tuple, list] = {}
_indicator_cache: dict[tuple, dict] = {}


def _normalize_dt(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _fetch_ohlcv_window(
    symbol: str,
    dt: datetime,
    hours_before: int = 96,
    hours_after: int = 200,
    timeframe: str = "1h",
):
    key = (symbol, _normalize_dt(dt).strftime("%Y-%m-%d"), timeframe, hours_after)
    if key in _ohlcv_cache:
        return _ohlcv_cache[key]

    exchange = ccxt.gate({"enableRateLimit": True})
    since_ms = int((_normalize_dt(dt) - timedelta(hours=hours_before)).timestamp() * 1000)
    bar_hours = 4 if timeframe == "4h" else 1
    limit = min(int((hours_before + hours_after) / bar_hours) + 5, 1000)
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since_ms, limit=limit)
        _ohlcv_cache[key] = bars
        return bars
    except Exception as e:
        log(f"Historical OHLCV fetch failed for {symbol}: {e}", "WARNING")
        return []


def _bars_up_to(bars: list, target: datetime) -> list:
    target_ms = int(_normalize_dt(target).timestamp() * 1000)
    return [b for b in bars if b[0] <= target_ms]


def _bars_in_range(bars: list, start: datetime, end: datetime) -> list:
    start_ms = int(_normalize_dt(start).timestamp() * 1000)
    end_ms = int(_normalize_dt(end).timestamp() * 1000)
    return [b for b in bars if start_ms <= b[0] <= end_ms]


def _close_at_or_before(bars: list, target: datetime) -> float | None:
    if not bars:
        return None
    target_ms = int(_normalize_dt(target).timestamp() * 1000)
    price = None
    for ts, _open, _high, _low, close, _vol in bars:
        if ts <= target_ms:
            price = float(close)
        else:
            break
    return price


def get_price_at_time(symbol: str, dt: datetime) -> float | None:
    bars = _fetch_ohlcv_window(symbol, dt)
    return _close_at_or_before(bars, dt)


def get_return_pct(signal_price: float, exit_price: float) -> float:
    if not signal_price or signal_price <= 0 or not exit_price:
        return 0.0
    return ((exit_price / signal_price) - 1) * 100


def get_path_extremes(symbol: str, start: datetime, end: datetime) -> tuple[float | None, float | None]:
    """Highest high and lowest low between start and end (inclusive)."""
    bars = _fetch_ohlcv_window(symbol, start, hours_after=int((end - start).total_seconds() / 3600) + 2)
    window = _bars_in_range(bars, start, end)
    if not window:
        return None, None
    highs = [float(b[2]) for b in window]
    lows = [float(b[3]) for b in window]
    return max(highs), min(lows)


def check_target_hit(
    action: str,
    signal_price: float,
    target_price: float,
    max_high: float,
    min_low: float,
    tolerance_pct: float = 0.5,
) -> bool:
    if not target_price or not signal_price:
        return False
    tol = tolerance_pct / 100.0
    if action == "BUY":
        threshold = target_price * (1 - tol)
        return max_high is not None and max_high >= threshold
    if action == "SELL":
        threshold = target_price * (1 + tol)
        return min_low is not None and min_low <= threshold
    return False


def get_indicators_at_time(symbol: str, dt: datetime, timeframe: str = "4h") -> dict | None:
    key = (symbol, _normalize_dt(dt).isoformat(), timeframe)
    if key in _indicator_cache:
        return _indicator_cache[key]

    bar_hours = 4 if timeframe == "4h" else 1
    hours_before = 100 * bar_hours
    bars = _fetch_ohlcv_window(symbol, dt, hours_before=hours_before, hours_after=2, timeframe=timeframe)
    window = _bars_up_to(bars, dt)
    if len(window) < 25:
        return None

    df = pd.DataFrame(window, columns=["ts", "open", "high", "low", "close", "volume"])
    df["rsi"] = talib.RSI(df["close"], timeperiod=14)
    _, _, df["lower"] = talib.BBANDS(df["close"], timeperiod=20)
    df["vol_avg"] = df["volume"].rolling(window=20).mean()

    row = df.iloc[-1]
    recent_vol_avg = df["volume"].tail(4).mean()
    long_vol_avg = row["vol_avg"]
    vol_multiplier = recent_vol_avg / long_vol_avg if long_vol_avg and long_vol_avg > 0 else 1.0

    result = {
        "rsi": float(row["rsi"]) if pd.notna(row["rsi"]) else 45.0,
        "lower_bb": float(row["lower"]) if pd.notna(row["lower"]) else float(row["close"]) * 0.97,
        "vol_multiplier": float(vol_multiplier),
        "close": float(row["close"]),
    }
    _indicator_cache[key] = result
    return result


def prefetch_for_posts(symbol_times: list[tuple[str, datetime]], hold_days: int = 7):
    """Warm OHLCV cache for upcoming point-in-time and path lookups."""
    seen: set[tuple] = set()
    hours_after = hold_days * 24 + 8
    for symbol, dt in symbol_times:
        key = (symbol, _normalize_dt(dt).strftime("%Y-%m-%d"), "1h", hours_after)
        if key in seen:
            continue
        seen.add(key)
        _fetch_ohlcv_window(symbol, dt, hours_after=hours_after)
        _fetch_ohlcv_window(symbol, dt, hours_after=2, timeframe="4h")


def clear_cache():
    _ohlcv_cache.clear()
    _indicator_cache.clear()