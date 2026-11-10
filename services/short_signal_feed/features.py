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
    RSI_PERIOD,
    RSI_PERIOD_SOURCE,
)
from strategies.climax_fade import normalize_climax_symbol

_NUMBER = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")


def decimal_input(value: object) -> Decimal | None:
    """Accept an int or a decimal/scientific string. Floats stay out of prices."""
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


def funding_number(value: object) -> Decimal | None:
    """Funding rates may be JSON numbers or strings such as ``1e-4``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            return None
        return number if number.is_finite() else None
    return decimal_input(value)


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


def _partition_bars(symbol: str, as_of, bars: list) -> tuple[list[tuple], int]:
    """Closed bars at ``as_of``, plus how many close after ``as_of``.

    A bar whose open is at or before ``as_of`` but whose close is after
    ``as_of`` is still open. It is counted and excluded. Using the open
    time for that comparison would admit it.
    """
    closed: list[tuple] = []
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
        close_at = opened + timedelta(hours=BAR_HOURS)
        if close_at > as_of:
            excluded += 1
            continue
        closed.append((opened, bar))
    return closed, excluded


def _rsi_block(reason: str | None, value: str | None, available: int, used: int) -> dict:
    return {
        "period": RSI_PERIOD,
        "period_source": RSI_PERIOD_SOURCE,
        "value": value,
        "reason": reason,
        "threshold": None,
        "n_closes": used,
        "n_closes_available": available,
    }


def _volume_and_rsi(
    closed: list[tuple],
    *,
    excluded_after_as_of: int,
) -> tuple[dict, dict, list[str]]:
    missing: list[str] = []
    grouped: dict = {}
    for opened, bar in closed:
        grouped.setdefault(opened, []).append(bar)
    duplicate_times = [opened for opened, items in grouped.items() if len(items) > 1]
    if duplicate_times:
        names = [item.strftime("%Y-%m-%dT%H:%M:%SZ") for item in sorted(duplicate_times)]
        rsi = _rsi_block("duplicate_open_time", None, 0, 0)
        missing.append("duplicate_open_time")
        volume = {
            "bars": [],
            "consecutive_ratios": [],
            "excluded_after_as_of": excluded_after_as_of,
            "duplicate_open_time": True,
            "duplicate_open_times": names,
        }
        return rsi, volume, missing

    ordered = sorted(grouped)
    listed = []
    closes: list[Decimal] = []
    bad_close = False
    for opened in ordered:
        bar = grouped[opened][0]
        close = decimal_input(bar.get("close"))
        volume_value = decimal_input(bar.get("volume"))
        if close is None:
            bad_close = True
        else:
            closes.append(close)
        listed.append(
            {
                "open_time": bar.get("open_time"),
                "close": None if close is None else _format_decimal(close),
                "volume": None if volume_value is None else _format_decimal(volume_value),
                "_opened": opened,
            }
        )
    ratios = []
    gapped = False
    for prev, current in zip(listed, listed[1:]):
        step = current["_opened"] - prev["_opened"]
        if step != timedelta(hours=BAR_HOURS):
            gapped = True
            ratios.append(
                {
                    "open_time": current["open_time"],
                    "prev_open_time": prev["open_time"],
                    "ratio": None,
                    "reason": "non_consecutive",
                }
            )
            continue
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
    public_bars = [
        {"open_time": item["open_time"], "close": item["close"], "volume": item["volume"]}
        for item in listed
    ]
    available = len(closes)
    if bad_close:
        rsi = _rsi_block("non_decimal_close", None, available, 0)
        missing.append("non_decimal_close")
    elif gapped:
        rsi = _rsi_block("gap_in_closes", None, available, 0)
        missing.append("gap_in_closes")
    elif available < RSI_PERIOD + 1:
        rsi = _rsi_block("insufficient_closes", None, available, 0)
        missing.append("insufficient_closes")
    else:
        series = np.array([float(item) for item in closes], dtype=float)
        value = float(talib.RSI(series, timeperiod=RSI_PERIOD)[-1])
        if math.isnan(value):
            rsi = _rsi_block("insufficient_closes", None, available, 0)
            missing.append("insufficient_closes")
        else:
            rsi = _rsi_block(None, format(value, ".10f"), available, available)
    volume = {
        "bars": public_bars,
        "consecutive_ratios": ratios,
        "excluded_after_as_of": excluded_after_as_of,
        "duplicate_open_time": False,
        "duplicate_open_times": [],
    }
    return rsi, volume, missing


def _funding(symbol: str, as_of, funding: list) -> tuple[dict, list[str]]:
    history = []
    excluded = 0
    unparseable = 0
    for index, row in enumerate(funding):
        if not isinstance(row, dict):
            continue
        if normalize_climax_symbol(row.get("symbol")) != symbol:
            continue
        settled = parse_timestamp(row.get("settled_at"))
        if settled is None:
            continue
        if settled > as_of:
            excluded += 1
            continue
        rate = funding_number(row.get("rate"))
        if rate is None:
            unparseable += 1
            continue
        interval = (
            funding_number(row.get("interval_hours")) if "interval_hours" in row else None
        )
        if interval is None:
            interval_known = False
            interval_text = None
        else:
            interval_known = True
            interval_text = _format_decimal(interval)
        net = funding_number(row.get("net_cost_usdt")) if "net_cost_usdt" in row else None
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
    if unparseable:
        reason = "unparseable_funding"
    elif not rows:
        reason = "no_settlement_at_or_before_as_of"
    else:
        reason = None
    missing = [] if reason is None else [reason]
    return (
        {
            "last": rows[-1] if rows else None,
            "history": rows,
            "excluded_after_as_of": excluded,
            "unparseable_count": unparseable,
            "reason": reason,
        },
        missing,
    )


def raw_features(identity: dict, bars: list, funding: list, now_dt) -> dict:
    symbol = identity["symbol"]
    as_of = identity["as_of"]
    closed, excluded_bars = _partition_bars(symbol, as_of, bars)
    rsi, volume, rsi_missing = _volume_and_rsi(
        closed, excluded_after_as_of=excluded_bars
    )
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
