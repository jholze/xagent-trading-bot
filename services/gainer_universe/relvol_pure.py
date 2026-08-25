"""Relative-volume ignition detector (pure, no I/O).

Hypothesis (Früherkennung 10d): dead coins waking up show quote-vol expansion
vs their own baseline *before* they pass min_volume_usdt_24h=500k.

Signal at closed bar t (causal):
  qvol[t] >= mult * max(median(qvol[t-win:t]), baseline_floor)
  AND close[t] > open[t]
  AND qvol[t] >= min_ign_qvol

Bars: list of [ts_sec, open, high, low, close, base_vol] (ccxt-like)
or [ts_sec, quote_vol, close, high, low, open] (Gate REST) via qvol_from_bar.
"""

from __future__ import annotations

import math
from statistics import median
from typing import Any, Sequence


def qvol_ccxt(bar: Sequence[float]) -> float:
    """Quote volume ≈ base_vol * typical price for ccxt [ts,o,h,l,c,base_vol]."""
    o, h, l, c, v = float(bar[1]), float(bar[2]), float(bar[3]), float(bar[4]), float(bar[5])
    return v * ((o + h + l + c) / 4.0)


def qvol_gate_rest(bar: Sequence[float]) -> float:
    """Gate REST candlestick: [t, quote_vol, close, high, low, open, ...]."""
    return float(bar[1] or 0)


def detect_ignition_at_t(
    qs: Sequence[float],
    opens: Sequence[float],
    closes: Sequence[float],
    t: int,
    *,
    mult: float = 10.0,
    win: int = 12,
    baseline_floor: float = 100.0,
    min_ign_qvol: float = 5_000.0,
    min_nonzero: float = 0.25,
    require_green: bool = True,
) -> dict[str, Any] | None:
    """If bar index t is an ignition, return metrics dict; else None."""
    if t < win or t >= len(qs):
        return None
    prior = list(qs[t - win : t])
    need_nonzero = max(1, int(math.ceil(min_nonzero * win)))
    if sum(1 for q in prior if q > 0) < need_nonzero:
        return None
    base = max(median(prior), baseline_floor)
    q = float(qs[t] or 0)
    if q < mult * base:
        return None
    if require_green and float(closes[t]) <= float(opens[t]):
        return None
    if q < min_ign_qvol:
        return None
    factor = q / base if base > 0 else 0.0
    return {
        "t_index": t,
        "qvol": round(q, 2),
        "baseline": round(base, 2),
        "factor": round(factor, 2),
        "open": float(opens[t]),
        "close": float(closes[t]),
        "green": float(closes[t]) > float(opens[t]),
    }


def find_signals_ccxt(
    symbol: str,
    bars: list[Sequence[float]],
    *,
    mult: float = 10.0,
    win: int = 12,
    cooldown_h: int = 8,
    min_ign_qvol: float = 5_000.0,
    baseline_floor: float = 100.0,
    min_nonzero: float = 0.25,
    require_green: bool = True,
    only_last_closed: bool = False,
) -> list[dict[str, Any]]:
    """All ignitions in a series. If only_last_closed, only evaluate t=len-1."""
    if len(bars) <= win:
        return []
    qs = [qvol_ccxt(b) for b in bars]
    opens = [float(b[1]) for b in bars]
    closes = [float(b[4]) for b in bars]
    out: list[dict[str, Any]] = []
    last_sig = -10**9
    indices = [len(bars) - 1] if only_last_closed else range(win, len(bars))
    for t in indices:
        if t < win:
            continue
        if t - last_sig < cooldown_h:
            continue
        hit = detect_ignition_at_t(
            qs,
            opens,
            closes,
            t,
            mult=mult,
            win=win,
            baseline_floor=baseline_floor,
            min_ign_qvol=min_ign_qvol,
            min_nonzero=min_nonzero,
            require_green=require_green,
        )
        if not hit:
            continue
        last_sig = t
        ts = int(bars[t][0])
        # ccxt ms vs sec
        if ts > 10_000_000_000:
            ts = ts // 1000
        out.append(
            {
                "symbol": symbol,
                "ts": ts,
                "variant": f"1h_{int(mult)}x_{win}hmed",
                **hit,
            }
        )
    return out


def abs_vol_24h_from_qs(qs: Sequence[float], end_i: int) -> float:
    """Sum of last up-to-24 hours of quote vol ending at end_i inclusive."""
    start = max(0, end_i - 23)
    return float(sum(qs[start : end_i + 1]))
