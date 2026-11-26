"""Observe-only EMA48/EMA480 trend short (#836).

Never places an order and never takes a slot. A failure is a log line.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

_EVALUATED: dict[str, int] = {}
_ERROR_LOGGED: dict[str, int] = {}
_TIMEFRAME_WARNED = False


def reset_for_tests() -> None:
    global _TIMEFRAME_WARNED
    _EVALUATED.clear()
    _ERROR_LOGGED.clear()
    _TIMEFRAME_WARNED = False


def _finite(value: Any) -> float | None:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num != num or num in (float("inf"), float("-inf")):
        return None
    return num


def _round_half_up(value: float) -> int:
    return int(math.floor(float(value) + 0.5))


def timeframe_hours(timeframe: str | None) -> float | None:
    text = str(timeframe or "").strip().lower()
    if len(text) < 2:
        return None
    unit = text[-1]
    num = _finite(text[:-1])
    if num is None or num <= 0:
        return None
    if unit == "h":
        return num
    if unit == "m":
        return num / 60.0
    if unit == "d":
        return num * 24.0
    return None


def trend_short_shadow_config(raw: dict | None = None) -> dict[str, Any]:
    """Missing block stays off. ``enabled`` is on only for literal ``True``."""
    out: dict[str, Any] = {
        "enabled": False,
        "fast_hours": 48.0,
        "slow_hours": 480.0,
        "timeframe": "4h",
        "warmup_mult": 3.0,
        "round_trip_cost_pct": 0.7,
    }
    shorts = (raw or {}).get("shorts") if isinstance(raw, dict) else None
    block = shorts.get("trend_short_shadow") if isinstance(shorts, dict) else None
    if not isinstance(block, dict):
        return out
    out["enabled"] = block.get("enabled") is True
    for key in ("fast_hours", "slow_hours", "warmup_mult", "round_trip_cost_pct"):
        num = _finite(block.get(key))
        if num is not None and num > 0:
            out[key] = num
    tf = str(block.get("timeframe") or "").strip()
    if tf:
        out["timeframe"] = tf
    return out


def ema_spans(cfg: dict | None) -> tuple[int, int, int] | None:
    """``(fast_span, slow_span, bars)``. Bars is ``warmup_mult * slow_span``.

    None when the hours do not yield a fast span shorter than the slow span.
    """
    cfg = cfg or {}
    hours = timeframe_hours(str(cfg.get("timeframe") or "4h"))
    fast_h = _finite(cfg.get("fast_hours"))
    slow_h = _finite(cfg.get("slow_hours"))
    mult = _finite(cfg.get("warmup_mult"))
    if hours is None or fast_h is None or slow_h is None or mult is None:
        return None
    if fast_h <= 0 or slow_h <= 0 or mult <= 0:
        return None
    fast = _round_half_up(fast_h / hours)
    slow = _round_half_up(slow_h / hours)
    if fast < 1 or slow <= fast:
        return None
    bars = _round_half_up(mult * slow)
    if bars < slow + 1:
        bars = slow + 1
    return fast, slow, bars


def ema_series(closes: list[float], span: int) -> list[float | None]:
    """EMA seeded with the SMA of the first ``span`` closes. Alpha is ``2/(span+1)``."""
    out: list[float | None] = [None] * len(closes)
    if span <= 0 or len(closes) < span:
        return out
    k = 2.0 / (span + 1)
    ema = sum(closes[:span]) / float(span)
    out[span - 1] = ema
    for i in range(span, len(closes)):
        ema = float(closes[i]) * k + ema * (1.0 - k)
        out[i] = ema
    return out


def ema_cross(
    fast_prev: float | None,
    slow_prev: float | None,
    fast_cur: float | None,
    slow_cur: float | None,
) -> str | None:
    """``would_open`` when fast crosses below slow. ``would_cover`` when it crosses above."""
    if None in (fast_prev, slow_prev, fast_cur, slow_cur):
        return None
    if fast_prev >= slow_prev and fast_cur < slow_cur:
        return "would_open"
    if fast_prev <= slow_prev and fast_cur > slow_cur:
        return "would_cover"
    return None


def _fmt(value: Any) -> str:
    num = _finite(value)
    if num is None:
        return "na"
    text = f"{num:.6f}".rstrip("0").rstrip(".")
    if text in ("", "-", "-0"):
        return "0"
    return text


def format_trend_shadow_line(
    event: str,
    *,
    symbol: str,
    close: float,
    ema_fast: float,
    ema_slow: float,
    s: float | None,
    excluded: bool,
    n_bars: int,
) -> str:
    return (
        f"trend_short_shadow {event} symbol={symbol} close={_fmt(close)} "
        f"ema_fast={_fmt(ema_fast)} ema_slow={_fmt(ema_slow)} s={_fmt(s)} "
        f"n_bars={int(n_bars)} excluded={'true' if excluded else 'false'}"
    )


def _emit(line: str, level: str = "INFO") -> None:
    try:
        from logger import log

        log(line, level)
    except Exception:
        return


def _read_ohlcv(ohlcv) -> tuple[list[float], list[int]] | None:
    if ohlcv is None:
        return None
    empty = getattr(ohlcv, "empty", False)
    if empty is True or (empty is not False and bool(empty)):
        return None
    try:
        closes = [float(x) for x in ohlcv["close"].tolist()]
        stamps = [int(x) for x in ohlcv["ts"].tolist()]
    except Exception:
        return None
    if not closes or len(closes) != len(stamps):
        return None
    return closes, stamps


def _published_score(now_ts: float) -> tuple[float | None, float | None]:
    """Smoothed score and its age in seconds, from the #835 reader. Never raises."""
    try:
        from services.market_oracle.score_runtime import read_published_score

        block = read_published_score()
    except Exception:
        return None, None
    if not isinstance(block, dict):
        return None, None
    s = _finite(block.get("smoothed"))
    ts = _finite(block.get("ts"))
    age = None if s is None or ts is None else now_ts - ts
    return s, age


def _log_error_once(symbol: str, bar_ms: int, exc: BaseException) -> None:
    if _ERROR_LOGGED.get(symbol) == bar_ms:
        return
    _ERROR_LOGGED[symbol] = bar_ms
    _emit(f"trend_short_shadow error symbol={symbol}: {exc}", "ERROR")


def _warn_timeframe_once(timeframe: Any) -> None:
    """One WARNING for a non-4h timeframe. The shadow stays off."""
    global _TIMEFRAME_WARNED
    if _TIMEFRAME_WARNED:
        return
    _TIMEFRAME_WARNED = True
    shown = str(timeframe or "").strip() or "na"
    _emit(
        f"trend_short_shadow disabled timeframe={shown} reason=only_4h",
        "WARNING",
    )


def _remember(symbol: str, bar_ms: int) -> None:
    """Negative cache: this symbol does not fetch again on this closed bar."""
    _EVALUATED[symbol] = bar_ms


def observe_trend_short(
    symbol: str,
    market,
    config_raw: dict | None,
    now: datetime | None = None,
) -> dict | None:
    """One closed-bar evaluation. Returns the obs row when a cross fires.

    Does not call the trading service. Returns None when disabled, when this
    bar was already evaluated, or when the frame has no cross.
    """
    try:
        return _observe(symbol, market, config_raw, now)
    except Exception as exc:
        try:
            from strategies.climax_fade import last_closed_4h_open_ms, normalize_climax_symbol

            now_dt = now or datetime.now(timezone.utc)
            sym = normalize_climax_symbol(symbol) or str(symbol or "")
            _log_error_once(sym, last_closed_4h_open_ms(now_dt), exc)
        except Exception:
            _emit(f"trend_short_shadow error symbol={symbol}: {exc}", "ERROR")
        return None


def _observe(symbol, market, config_raw, now) -> dict | None:
    from strategies.climax_fade import (
        climax_fade_config,
        is_closed_4h_bar,
        is_excluded_symbol,
        last_closed_4h_open_ms,
        normalize_climax_symbol,
    )

    cfg = trend_short_shadow_config(config_raw)
    if not cfg.get("enabled"):
        return None
    timeframe = str(cfg.get("timeframe") or "").strip().lower()
    if timeframe != "4h":
        _warn_timeframe_once(cfg.get("timeframe"))
        return None
    spans = ema_spans(cfg)
    if spans is None:
        return None
    fast_span, slow_span, bars = spans
    sym = normalize_climax_symbol(symbol) or str(symbol or "")
    if not sym:
        return None
    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    bar_ms = last_closed_4h_open_ms(now_dt)
    if _EVALUATED.get(sym) == bar_ms:
        return None
    if market is None or not hasattr(market, "fetch_ohlcv"):
        return None
    try:
        frame = market.fetch_ohlcv(sym, "4h", bars)
    except Exception as exc:
        _log_error_once(sym, bar_ms, exc)
        return None
    parsed = _read_ohlcv(frame)
    if parsed is None:
        _emit(f"trend_short_shadow no_data symbol={sym}", "INFO")
        _remember(sym, bar_ms)
        return None
    closes_all, stamps_all = parsed
    closed_closes: list[float] = []
    closed_ts: list[int] = []
    for close, ts in zip(closes_all, stamps_all):
        if is_closed_4h_bar(ts, now_dt):
            closed_closes.append(close)
            closed_ts.append(ts)
    if not closed_ts or int(closed_ts[-1]) != bar_ms:
        return None
    n_bars = len(closed_closes)
    if n_bars < bars:
        _remember(sym, bar_ms)
        return None
    _remember(sym, bar_ms)
    fast = ema_series(closed_closes, fast_span)
    slow = ema_series(closed_closes, slow_span)
    event = ema_cross(fast[-2], slow[-2], fast[-1], slow[-1])
    if event is None:
        return None
    close = float(closed_closes[-1])
    ema_fast = float(fast[-1])
    ema_slow = float(slow[-1])
    now_ts = float(now_dt.timestamp())
    s, age = _published_score(now_ts)
    excluded = is_excluded_symbol(sym, climax_fade_config(config_raw))
    line = format_trend_shadow_line(
        event,
        symbol=sym,
        close=close,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        s=s,
        excluded=excluded,
        n_bars=n_bars,
    )
    _emit(line, "INFO")
    row = {
        "kind": "trend_short_shadow",
        "symbol": sym,
        "event": event,
        "close": close,
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "s": s,
        "score_age_s": age,
        "excluded": excluded,
        "n_bars": n_bars,
        "bar_open_ts_ms": int(closed_ts[-1]),
        "timeframe": "4h",
        "fast_span": fast_span,
        "slow_span": slow_span,
        "round_trip_cost_pct": float(cfg.get("round_trip_cost_pct") or 0.7),
        "ts": now_ts,
    }
    try:
        from services.observe_streams import mirror_record

        mirror_record("obs_trend_short_shadow", row)
    except Exception as exc:
        _emit(f"trend_short_shadow obs error symbol={sym}: {exc}", "ERROR")
    return row
