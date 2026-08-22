"""Candles + BB(20) + RSI(14) for the desk chart. Warmup stays None (UI gap)."""

from __future__ import annotations

from typing import Any

import pandas as pd

try:
    import talib
except ImportError:  # pragma: no cover - test env normally has talib
    talib = None

# Last close is at/near the lower band when close <= lower * 1.002. None while BB is warming up.
_AT_LOWER_BB_FACTOR = 1.002


def _unavailable() -> dict:
    return {"ok": False, "error": "ohlcv_unavailable", "bars": []}


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def _optional_floats(values) -> list[float | None]:
    return [_as_float(v) for v in values]


def _bar(row: Any) -> dict:
    src = row if isinstance(row, dict) else {}
    return {
        "ts": src.get("ts"),
        "open": _as_float(src.get("open")),
        "high": _as_float(src.get("high")),
        "low": _as_float(src.get("low")),
        "close": _as_float(src.get("close")),
    }


def _pandas_rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi = rsi.mask(avg_loss == 0.0, 100.0)
    rsi.iloc[:period] = pd.NA
    return rsi


def _pandas_bbands(close: pd.Series, period: int) -> tuple[pd.Series, pd.Series, pd.Series]:
    middle = close.rolling(window=period, min_periods=period).mean()
    std = close.rolling(window=period, min_periods=period).std(ddof=0)
    upper = middle + 2.0 * std
    lower = middle - 2.0 * std
    return upper, middle, lower


def _rsi_and_bb(
    closes: list[float | None],
    *,
    rsi_period: int,
    bb_period: int,
) -> tuple[list[float | None], list[float | None], list[float | None], list[float | None]]:
    close = pd.Series(
        [c if c is not None else float("nan") for c in closes],
        dtype="float64",
    )
    if talib is not None:
        rsi_raw = talib.RSI(close, timeperiod=rsi_period)
        upper_raw, middle_raw, lower_raw = talib.BBANDS(close, timeperiod=bb_period)
    else:
        rsi_raw = _pandas_rsi(close, rsi_period)
        upper_raw, middle_raw, lower_raw = _pandas_bbands(close, bb_period)
    return (
        _optional_floats(rsi_raw),
        _optional_floats(upper_raw),
        _optional_floats(middle_raw),
        _optional_floats(lower_raw),
    )


def build_ohlcv_pack(
    rows: list[dict] | None,
    *,
    rsi_period: int = 14,
    bb_period: int = 20,
) -> dict:
    """Candles + BB(20) + RSI(14). Warmup = None (UI draws a gap)."""
    if not rows:
        return _unavailable()

    bars = [_bar(row) for row in rows]
    closes = [bar["close"] for bar in bars]
    rsi, bb_upper, bb_middle, bb_lower = _rsi_and_bb(
        closes,
        rsi_period=int(rsi_period),
        bb_period=int(bb_period),
    )
    last_close = closes[-1] if closes else None
    last_lower = bb_lower[-1] if bb_lower else None
    last_rsi = rsi[-1] if rsi else None
    if last_close is None or last_lower is None:
        at_lower_bb = None
    else:
        at_lower_bb = bool(last_close <= last_lower * _AT_LOWER_BB_FACTOR)

    return {
        "ok": True,
        "closes": closes,
        "rsi": rsi,
        "bb_upper": bb_upper,
        "bb_middle": bb_middle,
        "bb_lower": bb_lower,
        "bars": bars,
        "last_rsi": last_rsi,
        "at_lower_bb": at_lower_bb,
    }


def _rows_from_frame(df: pd.DataFrame) -> list[dict]:
    rows: list[dict] = []
    for rec in df.to_dict("records"):
        rows.append(
            {
                "ts": rec.get("ts"),
                "open": rec.get("open"),
                "high": rec.get("high"),
                "low": rec.get("low"),
                "close": rec.get("close"),
            }
        )
    return rows


def load_ohlcv(symbol: str, tf: str, limit: int = 120) -> dict:
    """Live candles via MarketService. Fail-open — never raise to HTTP."""
    try:
        from services.market_service import MarketService

        config_raw = None
        try:
            from core.config import get_bot_config

            config_raw = get_bot_config().raw
        except Exception:
            config_raw = None
        df = MarketService(config_raw)._fetch_ohlcv(symbol, tf, int(limit))
        if df is None or getattr(df, "empty", True):
            return _unavailable()
        rows = _rows_from_frame(df)
        if not rows:
            return _unavailable()
        return build_ohlcv_pack(rows)
    except Exception:
        return _unavailable()
