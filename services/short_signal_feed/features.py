"""Raw funding, RSI(14), and consecutive volume ratios. No cutoff is applied."""

from __future__ import annotations

import math
import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation

import numpy as np
import talib

from services.short_signal_feed.reader import parse_timestamp
from services.short_signal_feed.schema import (
    BAR_HOURS,
    HOLDOUT_START,
    RSI_PERIOD,
    RSI_PERIOD_SOURCE,
)
from strategies.climax_fade import normalize_climax_symbol

_NUMBER = re.compile(r"^[+-]?(?:0|[1-9]\d*)(?:\.\d+)?$")


def decimal_input(value: object) -> Decimal | None:
    """Accept an int or a decimal string. Floats and booleans are not exact."""
    if isinstance(value, bool) or isinstance(value, float):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        text = value.strip()
        if _NUMBER.fullmatch(text) is None:
            return None
        try:
            number = Decimal(text)
        except InvalidOperation:
            return None
        if not number.is_finite():
            return None
        return number
    return None


def _format_decimal(number: Decimal) -> str:
    return format(number, "f")


def _optional_text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _eligibility_inputs(record: dict) -> dict:
    kind = record.get("instrument_kind")
    if not isinstance(kind, str) or not kind.strip():
        kind = None
    else:
        kind = kind.strip()

    def flag(name: str):
        if name not in record or record.get(name) is None:
            return None
        value = record.get(name)
        return value if isinstance(value, bool) else None

    return {
        "instrument_kind": kind,
        "listed_perp": flag("listed_perp"),
        "calendar_known": flag("calendar_known"),
        "depth_known": flag("depth_known"),
    }


def _closed_bars(symbol: str, as_of, bars: list) -> tuple[list[dict], int]:
    chosen: dict = {}
    excluded = 0
    for bar in bars:
        if not isinstance(bar, dict):
            continue
        if normalize_climax_symbol(bar.get("symbol")) != symbol:
            continue
        if bar.get("timeframe") != "4h":
            continue
        opened = parse_timestamp(bar.get("open_time"))
        if opened is None:
            continue
        closed = opened + timedelta(hours=BAR_HOURS)
        if closed > as_of or closed >= HOLDOUT_START:
            if closed > as_of:
                excluded += 1
            continue
        if opened in chosen:
            continue
        chosen[opened] = bar
    return [chosen[item] for item in sorted(chosen)], excluded


def _volume_and_rsi(rows: list[dict]) -> tuple[dict, dict, list[str]]:
    missing: list[str] = []
    listed = []
    closes: list[Decimal] = []
    bad_close = False
    for bar in rows:
        close = decimal_input(bar.get("close"))
        volume = decimal_input(bar.get("volume"))
        if close is None:
            bad_close = True
        else:
            closes.append(close)
        listed.append(
            {
                "open_time": bar.get("open_time"),
                "close": None if close is None else _format_decimal(close),
                "volume": None if volume is None else _format_decimal(volume),
            }
        )
    ratios = []
    for prev, current in zip(listed, listed[1:]):
        left = decimal_input(prev["volume"]) if prev["volume"] is not None else None
        right = decimal_input(current["volume"]) if current["volume"] is not None else None
        if left is None or right is None or left <= 0:
            ratios.append(
                {
                    "open_time": current["open_time"],
                    "prev_open_time": prev["open_time"],
                    "ratio": None,
                    "reason": "zero_or_missing_volume",
                }
            )
            continue
        ratios.append(
            {
                "open_time": current["open_time"],
                "prev_open_time": prev["open_time"],
                "ratio": _format_decimal(right / left),
                "reason": None,
            }
        )
    if bad_close:
        rsi = {
            "period": RSI_PERIOD,
            "period_source": RSI_PERIOD_SOURCE,
            "value": None,
            "reason": "non_decimal_close",
            "threshold": None,
        }
        missing.append("non_decimal_close")
    elif len(closes) < RSI_PERIOD + 1:
        rsi = {
            "period": RSI_PERIOD,
            "period_source": RSI_PERIOD_SOURCE,
            "value": None,
            "reason": "insufficient_closes",
            "threshold": None,
        }
        missing.append("insufficient_closes")
    else:
        series = np.array([float(item) for item in closes], dtype=float)
        value = float(talib.RSI(series, timeperiod=RSI_PERIOD)[-1])
        if math.isnan(value):
            rsi = {
                "period": RSI_PERIOD,
                "period_source": RSI_PERIOD_SOURCE,
                "value": None,
                "reason": "insufficient_closes",
                "threshold": None,
            }
            missing.append("insufficient_closes")
        else:
            rsi = {
                "period": RSI_PERIOD,
                "period_source": RSI_PERIOD_SOURCE,
                "value": format(value, ".10f"),
                "reason": None,
                "threshold": None,
            }
    volume = {"bars": listed, "consecutive_ratios": ratios}
    return rsi, volume, missing


def _funding(symbol: str, as_of, funding: list) -> tuple[dict, list[str]]:
    history = []
    excluded = 0
    for index, row in enumerate(funding):
        if not isinstance(row, dict):
            continue
        if normalize_climax_symbol(row.get("symbol")) != symbol:
            continue
        settled = parse_timestamp(row.get("settled_at"))
        if settled is None:
            continue
        if settled > as_of or settled >= HOLDOUT_START:
            if settled > as_of:
                excluded += 1
            continue
        rate = decimal_input(row.get("rate"))
        if rate is None:
            continue
        interval = decimal_input(row.get("interval_hours")) if "interval_hours" in row else None
        if "interval_hours" in row and row.get("interval_hours") is not None and interval is None:
            interval_known = False
            interval_text = None
        elif interval is None:
            interval_known = False
            interval_text = None
        else:
            interval_known = True
            interval_text = _format_decimal(interval)
        net = decimal_input(row.get("net_cost_usdt")) if "net_cost_usdt" in row else None
        history.append(
            (
                settled,
                index,
                {
                    "settled_at": row.get("settled_at"),
                    "rate": _format_decimal(rate),
                    "interval_hours": interval_text,
                    "interval_known": interval_known,
                    "provenance": _optional_text(row.get("provenance")),
                    "quote": _optional_text(row.get("quote")),
                    "net_cost_usdt": None if net is None else _format_decimal(net),
                },
            )
        )
    history.sort(key=lambda item: (item[0], item[1]))
    rows = [item[2] for item in history]
    reason = None if rows else "no_settlement_at_or_before_as_of"
    missing = [] if rows else [reason]
    return (
        {
            "last": rows[-1] if rows else None,
            "history": rows,
            "excluded_after_as_of": excluded,
            "reason": reason,
        },
        missing,
    )


def raw_features(identity: dict, bars: list, funding: list, now_dt) -> dict:
    symbol = identity["symbol"]
    as_of = identity["as_of"]
    closed, _excluded = _closed_bars(symbol, as_of, bars)
    rsi, volume, rsi_missing = _volume_and_rsi(closed)
    funding_block, funding_missing = _funding(symbol, as_of, funding)
    if now_dt is None:
        age = {"seconds": None, "reason": "now_not_supplied"}
        age_missing = ["now_not_supplied"]
    else:
        age = {
            "seconds": str(int((now_dt - as_of).total_seconds())),
            "reason": None,
        }
        age_missing = []
    missing = rsi_missing + funding_missing + age_missing
    return {
        "features": {
            "rsi": rsi,
            "funding": funding_block,
            "volume": volume,
            "age": age,
        },
        "missing_reasons": missing,
        "inputs": _eligibility_inputs(identity["record"]),
    }
