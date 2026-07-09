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


def _bar_ms(timeframe: str) -> int:
    return {
        "15m": 900_000,
        "30m": 1_800_000,
        "1h": 3_600_000,
        "4h": 14_400_000,
    }.get(timeframe, 3_600_000)


def _gate_exchange():
    return ccxt.gate({"enableRateLimit": True})


def _dedupe_bars(bars: list) -> list:
    by_ts: dict[int, list] = {}
    for bar in bars:
        by_ts[int(bar[0])] = bar
    return [by_ts[ts] for ts in sorted(by_ts)]


def _fetch_ohlcv_range(
    symbol: str,
    start: datetime,
    end: datetime,
    timeframe: str = "1h",
) -> list:
    start = _normalize_dt(start)
    end = _normalize_dt(end)
    key = (symbol, start.isoformat(), end.isoformat(), timeframe)
    if key in _ohlcv_cache:
        return _ohlcv_cache[key]

    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    bar_ms = _bar_ms(timeframe)
    since_ms = start_ms
    merged: list = []

    try:
        exchange = _gate_exchange()
        while since_ms < end_ms:
            chunk = exchange.fetch_ohlcv(
                symbol, timeframe=timeframe, since=since_ms, limit=1000,
            )
            if not chunk:
                break
            merged.extend(chunk)
            last_ts = int(chunk[-1][0])
            if last_ts <= since_ms:
                break
            since_ms = last_ts + bar_ms
            if len(chunk) < 1000:
                break
    except Exception as e:
        log(f"Historical OHLCV fetch failed for {symbol}: {e}", "WARNING")
        return []

    bars = [b for b in _dedupe_bars(merged) if start_ms <= int(b[0]) <= end_ms]
    _ohlcv_cache[key] = bars
    return bars


def _fetch_ohlcv_window(
    symbol: str,
    dt: datetime,
    hours_before: int = 96,
    hours_after: int = 200,
    timeframe: str = "1h",
):
    anchor = _normalize_dt(dt)
    start = anchor - timedelta(hours=hours_before)
    end = anchor + timedelta(hours=hours_after)
    return _fetch_ohlcv_range(symbol, start, end, timeframe=timeframe)


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
    seen: set[tuple[str, str]] = set()
    hours_after = hold_days * 24 + 8
    for symbol, dt in symbol_times:
        anchor = _normalize_dt(dt)
        start = (anchor - timedelta(hours=4)).isoformat()
        end = (anchor + timedelta(hours=hours_after)).isoformat()
        dedupe_key = (symbol, start, end, "1h")
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        _fetch_ohlcv_window(symbol, dt, hours_before=4, hours_after=hours_after)
        _fetch_ohlcv_window(symbol, dt, hours_before=4, hours_after=2, timeframe="4h")


def clear_cache():
    _ohlcv_cache.clear()
    _indicator_cache.clear()