"""Diagnostic context-axis helpers. Write-only — never a risk/decision input.

``compute_volume_rel`` is pure (no I/O). ``read_oracle_state`` is a guarded
snapshot read with a lazy store import.
"""

from __future__ import annotations

import math

# bars covering one calendar day for the watchlist timeframes.
_BARS_PER_DAY = {
    "15m": 96,
    "1h": 24,
    "4h": 6,
    "1d": 1,
}


def _bars_per_day(timeframe) -> int | None:
    if timeframe is None:
        return None
    key = str(timeframe).strip().lower()
    n = _BARS_PER_DAY.get(key)
    return n if n and n > 0 else None


def _tail(series, n: int):
    if hasattr(series, "iloc"):
        return series.iloc[-n:]
    return series[-n:]


def compute_volume_rel(df, timeframe) -> float | None:
    """mean(volume of bars covering the last 24h) / mean(volume of bars covering the trailing 30 days).

    Bars-per-day are derived from the frame's timeframe (15m→96, 1h→24, 4h→6,
    1d→1). If the frame has fewer bars than 30 days, use the whole frame
    provided it covers ≥ 7 days; otherwise None. None also for None/empty df,
    missing ``volume`` column, or a zero/NaN denominator. Rounded to 4 decimals.
    """
    if df is None:
        return None
    bpd = _bars_per_day(timeframe)
    if bpd is None:
        return None
    try:
        empty = getattr(df, "empty", None)
        if empty is True:
            return None
        n = len(df)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    columns = getattr(df, "columns", None)
    try:
        has_volume = columns is not None and "volume" in columns
    except TypeError:
        has_volume = False
    if not has_volume:
        return None

    need_1 = bpd
    need_7 = bpd * 7
    need_30 = bpd * 30
    if n < need_7:
        return None

    try:
        vol = df["volume"]
        recent = _tail(vol, need_1)
        window = vol if n < need_30 else _tail(vol, need_30)
        num = float(recent.mean())
        den = float(window.mean())
    except (TypeError, ValueError, KeyError, AttributeError):
        return None
    if den == 0.0 or math.isnan(den) or math.isnan(num):
        return None
    return round(num / den, 4)


def read_oracle_state() -> str | None:
    """Market-oracle snapshot ``state`` (fallback key ``regime``), uppercased.

    Empty / missing snapshot → None. Store unavailable or raising → None,
    logged at DEBUG. Imports the store lazily so callers without Redis still
    import this module.
    """
    try:
        from services.market_oracle.store import get_latest_snapshot

        snap = get_latest_snapshot()
        if not snap:
            return None
        raw = snap.get("state") or snap.get("regime") or ""
        text = str(raw).strip().upper()
        return text or None
    except Exception as exc:
        from logger import log

        log(f"read_oracle_state: snapshot unavailable: {exc}", "DEBUG")
        return None
