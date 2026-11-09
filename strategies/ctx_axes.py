"""Diagnostic context-axis helpers. Write-only — never a risk/decision input.

``compute_volume_rel`` / ``compute_volume_rel_window`` are pure (no I/O).
``read_oracle_state`` is a guarded snapshot read with a lazy store import.
"""

from __future__ import annotations

import math


def bars_per_day_map() -> dict:
    """The shared bars-per-day map (``services.market_service.BARS_PER_DAY``).

    Imported lazily so ``import strategies.ctx_axes`` does not pull in
    ``market_service`` (ccxt/talib) at module import time (#375).
    """
    from services.market_service import BARS_PER_DAY

    return BARS_PER_DAY


def _bars_per_day(timeframe) -> int | None:
    if timeframe is None:
        return None
    key = str(timeframe).strip().lower()
    n = bars_per_day_map().get(key)
    return n if n and n > 0 else None


def _tail(series, n: int):
    if hasattr(series, "iloc"):
        return series.iloc[-n:]
    return series[-n:]


def compute_volume_rel_window(df, timeframe) -> tuple[float | None, float | None]:
    """24h mean volume over min(trailing 30 days, whole frame), plus the window used.

    Bars-per-day come from ``services.market_service._24H_BARS`` (15m→96, 30m→48,
    1h→24, 2h→12, 4h→6, 6h→4, 12h→2, 1d→1). Unknown timeframes → ``(None, None)``.

    Denominator is the mean volume of the last 30 days of bars when the frame
    covers that much, otherwise the whole frame provided it covers ≥ 7 days.
    Below 7 days, missing ``volume``, empty/None df, or a zero/NaN denominator
    → ``(None, None)``. Ratio rounded to 4 decimals.

    The second element is the effective window in days, reported beside the
    value as ``ctx_volume_window_days``: ``30.0`` on the tail-30d path, else
    ``round(n / bars_per_day, 2)`` for the whole-frame fallback. Live fetches
    are typically 300 bars, so 4h covers 30d while 1h covers only 12.5d.
    """
    if df is None:
        return None, None
    bpd = _bars_per_day(timeframe)
    if bpd is None:
        return None, None
    try:
        empty = getattr(df, "empty", None)
        if empty is True:
            return None, None
        n = len(df)
    except (TypeError, ValueError):
        return None, None
    if n <= 0:
        return None, None
    columns = getattr(df, "columns", None)
    try:
        has_volume = columns is not None and "volume" in columns
    except TypeError:
        has_volume = False
    if not has_volume:
        return None, None

    need_1 = bpd
    need_7 = bpd * 7
    need_30 = bpd * 30
    if n < need_7:
        return None, None

    try:
        vol = df["volume"]
        recent = _tail(vol, need_1)
        window = vol if n < need_30 else _tail(vol, need_30)
        num = float(recent.mean())
        den = float(window.mean())
    except (TypeError, ValueError, KeyError, AttributeError):
        return None, None
    if den == 0.0 or math.isnan(den) or math.isnan(num):
        return None, None
    volume_rel = round(num / den, 4)
    if n >= need_30:
        window_days: float | None = 30.0
    else:
        window_days = round(n / bpd, 2)
    return volume_rel, window_days


def compute_volume_rel(df, timeframe) -> float | None:
    """mean(volume of bars covering the last 24h) / mean(volume of bars covering min(30 days, whole frame)).

    Thin wrapper around ``compute_volume_rel_window`` (element 0). Semantics
    unchanged: whole-frame fallback when the frame has ≥ 7 and < 30 days;
    None below 7 days. The effective window is reported separately as
    ``ctx_volume_window_days`` (4h 300-bar live frame → 30d; 1h → 12.5d).
    Rounded to 4 decimals.
    """
    return compute_volume_rel_window(df, timeframe)[0]


def read_oracle_state() -> str | None:
    """Market-oracle snapshot ``state`` (fallback key ``regime``), uppercased.

    The order path reads Redis directly. A socket timeout raises
    ``OracleReadTimeout`` and does not fall back to the in-process cache.
    An empty snapshot or a connection failure returns None. Other errors
    return None and are logged at DEBUG.
    """
    from services.market_oracle.store import OracleReadTimeout, get_order_path_redis_snapshot

    try:
        snap = get_order_path_redis_snapshot()
    except OracleReadTimeout:
        raise
    except Exception as exc:
        from logger import log

        log(f"read_oracle_state: snapshot unavailable: {exc}", "DEBUG")
        return None
    if not snap:
        return None
    raw = snap.get("state") or snap.get("regime") or ""
    text = str(raw).strip().upper()
    return text or None


def entry_blocked_by_oracle_timeout(order) -> bool:
    """True when a new BUY/SHORT must not be sent because the oracle read timed out.

    Sells, covers, and stops are not blocked. An order that already carries
    ``ctx_oracle_state`` from a finished read is not blocked. A timeout does
    not consult the in-process cache.
    """
    side = str(getattr(order, "type", "") or "").upper()
    if side not in ("BUY", "SHORT"):
        return False
    if getattr(order, "ctx_oracle_state", None):
        return False
    from services.market_oracle.store import OracleReadTimeout

    try:
        read_oracle_state()
    except OracleReadTimeout:
        return True
    return False
