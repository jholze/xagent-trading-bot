"""Market regime series for regime-conditional backtests (2026-11-01).

Every series maps an hour-aligned unix ts T (a 1h bar open) to a regime label
computed ONLY from BTC/ETH bars that closed at or before T (no lookahead).

Definitions
-----------
oracle  The bot's own Market Oracle state machine (services.market_oracle.regime,
        ``raw_state_from_features`` + ``StateHysteresis``), fed with the price
        features the sidecar computes from Gate candles: BTC/ETH 24h return,
        BTC 1h / 4h return, 7d return, trend_4h from 4h EMA20/EMA50.
        Breadth and funding are not in the cache -> the price rules only (the
        bot does the same when those feeds are missing). Evaluated hourly
        instead of every 5 min. RISK_OFF/CRASH = risk-off.
sma     BTC last completed daily close < SMA(n) of completed daily closes.
ret     BTC return over the last n hours (last closed 1h bar) < 0.
"""

from __future__ import annotations

import bisect
from typing import Callable

from .policy import import_real

HOUR = 3600
DAY = 86400

OFF_STATES = frozenset({"RISK_OFF", "CRASH"})


def _ema(closes: list[float], period: int) -> float | None:
    if len(closes) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(closes[:period]) / float(period)
    for v in closes[period:]:
        e = v * k + e * (1.0 - k)
    return e


class Series:
    """Hourly regime labels, looked up by bar-open ts (floor to the hour)."""

    def __init__(self, name: str, points: dict[int, str]):
        self.name = name
        self.ts = sorted(points)
        self.lab = [points[t] for t in self.ts]

    def at(self, unix: int) -> str | None:
        t = int(unix) // HOUR * HOUR
        i = bisect.bisect_right(self.ts, t) - 1
        if i < 0:
            return None
        return self.lab[i]

    def risk_off(self, unix: int) -> bool:
        return (self.at(unix) or "") in OFF_STATES or self.at(unix) == "OFF"

    def gate(self) -> Callable[[int], bool]:
        return self.risk_off

    def flips(self, a: int, b: int) -> int:
        prev, n = None, 0
        for t, l in zip(self.ts, self.lab):
            if t < a or t >= b:
                continue
            off = l in OFF_STATES or l == "OFF"
            if prev is not None and off != prev:
                n += 1
            prev = off
        return n

    def share_off(self, a: int, b: int) -> float:
        xs = [(l in OFF_STATES or l == "OFF") for t, l in zip(self.ts, self.lab) if a <= t < b]
        return sum(xs) / len(xs) if xs else float("nan")

    def spells(self, a: int, b: int) -> list[tuple[bool, int]]:
        out: list[tuple[bool, int]] = []
        for t, l in zip(self.ts, self.lab):
            if t < a or t >= b:
                continue
            off = l in OFF_STATES or l == "OFF"
            if out and out[-1][0] == off:
                out[-1] = (off, out[-1][1] + 1)
            else:
                out.append((off, 1))
        return out


def _closes_upto(c1h, t: int) -> list[tuple[int, float]]:
    return [(b[0], b[4]) for b in c1h if b[0] + HOUR <= t]


def breadth_table(universe: dict[str, list], *, top_n: int = 40) -> dict[int, tuple[float, float]]:
    """T -> (pct_green, median_24h) like MarketDataClient.fetch_breadth, from 1h bars closed by T.

    Universe = top N by trailing 24h quote volume (base vol * close) among the given pairs.
    """
    idx = {sym: {b[0]: (b[4], b[5]) for b in bars} for sym, bars in universe.items()}
    all_ts = sorted({t for bars in universe.values() for t in (b[0] for b in bars)})
    out: dict[int, tuple[float, float]] = {}
    for k_ts in all_ts:
        T = k_ts + HOUR
        cands = []
        for sym, m in idx.items():
            now = m.get(k_ts)
            old = m.get(k_ts - 24 * HOUR)
            if not now or not old or old[0] <= 0:
                continue
            qv = 0.0
            for h in range(24):
                r = m.get(k_ts - h * HOUR)
                if r:
                    qv += r[0] * r[1]
            if qv <= 0:
                continue
            cands.append((qv, (now[0] / old[0] - 1) * 100))
        if len(cands) < 8:
            continue
        cands.sort(key=lambda x: x[0], reverse=True)
        rets = sorted(r for _, r in cands[: max(8, top_n)])
        n = len(rets)
        green = sum(1 for r in rets if r > 0) / n
        med = rets[n // 2] if n % 2 else 0.5 * (rets[n // 2 - 1] + rets[n // 2])
        out[T] = (green, med)
    return out


def oracle_series(
    btc_1h, eth_1h, btc_1d, *, risk_off_24h: float = -3.0, crash_24h: float = -6.0, min_bars: int = 1,
    name: str = "oracle", breadth: dict[int, tuple[float, float]] | None = None,
    funding: list[tuple[int, float]] | None = None, breadth_risk_off_max_green: float = 0.35,
    breadth_rotten_max_green: float = 0.25,
) -> Series:
    reg = import_real("services.market_oracle.regime")
    hyst = reg.StateHysteresis(min_bars_to_flip=min_bars)
    bt = [b[0] for b in btc_1h]
    bc = [b[4] for b in btc_1h]
    eth = {b[0]: b[4] for b in eth_1h}
    d_ts = [b[0] for b in btc_1d]
    d_c = [b[4] for b in btc_1d]
    fund_ts = [x[0] for x in (funding or [])]
    pts: dict[int, str] = {}
    for j in range(200, len(bt)):
        T = bt[j]  # bar open; use closes of bars with open < T
        k = j - 1  # last closed bar
        px = bc[k]
        def back(h: int) -> float | None:
            i = k - h
            if i < 0 or bt[k] - bt[i] != h * HOUR:
                return None
            return bc[i]
        f: dict[str, float] = {}
        p24 = back(24)
        if p24:
            f["btc_ret_24h_pct"] = (px / p24 - 1) * 100
        e_now, e24 = eth.get(bt[k]), eth.get(bt[k] - 24 * HOUR)
        if e_now and e24:
            f["eth_ret_24h_pct"] = (e_now / e24 - 1) * 100
        p1 = back(1)
        if p1:
            f["btc_ret_1h_pct"] = (px / p1 - 1) * 100
        p4 = back(4)
        if p4:
            f["btc_ret_4h_pct"] = (px / p4 - 1) * 100
        # 7d: current price vs daily close 7 completed days back (sidecar: 1d closes incl. forming bar)
        di = bisect.bisect_right(d_ts, T - DAY) - 1  # last completed day
        if di - 6 >= 0:
            f["btc_ret_7d_pct"] = (px / d_c[di - 6] - 1) * 100
        # 4h closes: completed 4h buckets + forming bucket at current price
        cur4 = T // (4 * HOUR) * 4 * HOUR
        c4: list[float] = []
        for i in range(max(0, k - 60 * 4), k + 1):
            if (bt[i] + HOUR) % (4 * HOUR) == 0 and bt[i] + HOUR <= cur4:
                c4.append(bc[i])
        c4 = c4[-59:] + [px]
        e20, e50 = _ema(c4, 20), _ema(c4, 50)
        if e20 is not None and e50 is not None:
            c0 = c4[-1]
            f["btc_trend_4h"] = 1.0 if (c0 >= e20 and e20 >= e50) else (-1.0 if (c0 < e20 and e20 < e50) else 0.0)
        if breadth is not None and T in breadth:
            f["breadth_pct_green"], f["breadth_median_24h_pct"] = breadth[T]
        if funding:
            fi = bisect.bisect_right(fund_ts, T) - 1
            if fi >= 0:
                f["btc_funding_rate_pct"] = funding[fi][1]
        if "btc_ret_24h_pct" not in f:
            continue
        raw, _conf, _why = reg.raw_state_from_features(
            f, risk_off_24h=risk_off_24h, crash_24h=crash_24h,
            breadth_risk_off_max_green=breadth_risk_off_max_green, breadth_rotten_max_green=breadth_rotten_max_green,
        )
        state, _bars = hyst.update(raw)
        pts[T] = state
    return Series(name, pts)


def sma_series(btc_1d, btc_1h, *, n: int = 50, name: str | None = None) -> Series:
    d_ts = [b[0] for b in btc_1d]
    d_c = [b[4] for b in btc_1d]
    pts: dict[int, str] = {}
    for b in btc_1h:
        T = b[0]
        di = bisect.bisect_right(d_ts, T - DAY) - 1  # last day whose bar closed by T
        if di + 1 < n:
            continue
        sma = sum(d_c[di - n + 1 : di + 1]) / n
        pts[T] = "OFF" if d_c[di] < sma else "ON"
    return Series(name or f"sma{n}", pts)


def ret_series(btc_1h, *, hours: int = 168, thr_pct: float = 0.0, name: str | None = None) -> Series:
    bt = [b[0] for b in btc_1h]
    bc = [b[4] for b in btc_1h]
    pts: dict[int, str] = {}
    for j in range(hours + 1, len(bt)):
        k = j - 1
        i = k - hours
        if bt[k] - bt[i] != hours * HOUR:
            continue
        r = (bc[k] / bc[i] - 1) * 100
        pts[bt[j]] = "OFF" if r < thr_pct else "ON"
    return Series(name or f"ret{hours}h", pts)


def load_inputs(candle_dir, breadth_dir, funding_path):
    """BTC/ETH bars from the harness cache, breadth universe + Gate BTC funding (all public)."""
    import json
    from pathlib import Path

    from .data import CandleCache

    cc = CandleCache(Path(candle_dir))
    bc = CandleCache(Path(breadth_dir))
    uni = {}
    d = Path(breadth_dir) / "1h"
    for p in sorted(d.glob("*.json")):
        sym = p.stem.replace("_", "/")
        bars = bc.get(sym, "1h")
        if bars:
            uni[sym] = bars
    fund = sorted((int(r["t"]), float(r["r"]) * 100.0) for r in json.loads(Path(funding_path).read_text()))
    return {
        "btc_1h": cc.get("BTC/USDT", "1h"),
        "eth_1h": cc.get("ETH/USDT", "1h"),
        "btc_1d": cc.get("BTC/USDT", "1d"),
        "universe": uni,
        "funding": fund,
    }
