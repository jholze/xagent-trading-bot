"""Buy & Hold benchmark, monthly returns and entry timing for a harness run.

Inspired by Jesse 3.6 ("did your strategy really beat buy and hold?"): every
run is set next to simply holding a benchmark basket over the same window,
with the same capital and the same cost model.

* Capital base: peak deployed capital of the run (the same denominator as
  ``RunMetrics.max_dd_pct``) unless ``BenchmarkConfig.capital`` is set.
* Strategy equity = capital base + cumulative PnL of the run, sampled at every
  UTC day boundary and at the window end. The engine's ``pnl_curve`` is
  mark-to-market (realised proceeds + open amount at liquidation value net of
  fee + slippage, every 1h bar), so both sides use the same marking.
* Buy & Hold: the capital base is split equally across the basket symbols at
  the open of the first 1h bar at or after the window start, held to the last
  bar that closed before the window end. Entry pays fee + slippage, every mark
  is the liquidation value net of fee + slippage (the harness ``CostModel``).
* Baskets (configurable): the fixed benchmark symbols (default BTC only) and an
  equal-weight basket of the symbols the run actually traded.
* Sharpe: mean / stdev of daily equity returns times sqrt(periods_per_year).
* Monthly returns: calendar months in UTC, first and last month may be partial.
* Entry timing: lots entered inside the window by hour of day and weekday,
  both in UTC and in Europe/Berlin (or ``BenchmarkConfig.local_tz``).

Pure functions, no network, no Mongo.
"""

from __future__ import annotations

import bisect
import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, Sequence
from zoneinfo import ZoneInfo

UTC = timezone.utc
DAY = 86400
HOUR = 3600
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# Default fixed benchmark. Override with BenchmarkConfig(symbols=...) or the
# CLI flag --benchmark-symbols; nothing below depends on which symbol it is.
DEFAULT_BENCHMARK_SYMBOLS: tuple[str, ...] = ("BTC/USDT",)
TRADED_BASKET = "traded_equal_weight"

Candle = tuple  # (open_ts, open, high, low, close, volume)


@dataclass(frozen=True)
class BenchmarkConfig:
    symbols: tuple[str, ...] = DEFAULT_BENCHMARK_SYMBOLS
    include_traded_basket: bool = True
    capital: float | None = None  # None = peak deployed capital of the run
    periods_per_year: float = 365.0  # crypto trades every day
    bar_seconds: int = HOUR  # candles passed in are 1h bars
    max_entry_lag_s: int = DAY  # symbol excluded if its first bar opens later than this after the start
    local_tz: str = "Europe/Berlin"

    def baskets(self, traded: Sequence[str]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        if self.symbols:
            out["+".join(self.symbols)] = list(self.symbols)
        if self.include_traded_basket and traded:
            out[TRADED_BASKET] = sorted(set(traded))
        return out


# ------------------------------------------------------------------ helpers
def sample_points(start_ts: int, end_ts: int) -> list[int]:
    """UTC day boundaries strictly inside (start, end), plus start and end."""
    pts = [start_ts]
    d = start_ts - start_ts % DAY + DAY
    while d < end_ts:
        pts.append(d)
        d += DAY
    if end_ts > start_ts:
        pts.append(end_ts)
    return pts


def peak_deployed(capital_curve: dict[int, float]) -> float:
    """Peak of the running sum of ``capital_curve`` (engine deltas of cost basis, not absolute values)."""
    cap = peak = 0.0
    for ts in sorted(capital_curve):
        cap += capital_curve[ts]
        peak = max(peak, cap)
    return peak


def strategy_equity(pnl_curve: dict[int, float], points: Sequence[int], capital: float) -> list[float]:
    """Capital + cumulative PnL booked at hours before each sample point.

    PnL booked at or after the last point (the final liquidation of lots still
    open at the horizon) is added to the last point so the end value equals
    capital + total PnL.
    """
    items = sorted(pnl_curve.items())
    out: list[float] = []
    cum = 0.0
    k = 0
    for i, p in enumerate(points):
        last = i == len(points) - 1
        while k < len(items) and (last or items[k][0] < p):
            cum += items[k][1]
            k += 1
        out.append(capital + cum)
    return out


def daily_returns_from_equity(equity: Sequence[float]) -> list[float]:
    return [b / a - 1.0 for a, b in zip(equity, equity[1:]) if a > 0]


def sharpe(returns: Sequence[float], periods_per_year: float) -> float | None:
    if len(returns) < 2:
        return None
    sd = statistics.stdev(returns)
    if sd <= 0:
        return None
    return statistics.fmean(returns) / sd * math.sqrt(periods_per_year)


def max_drawdown_pct(equity: Sequence[float]) -> float:
    peak = -math.inf
    dd = 0.0
    for v in equity:
        peak = max(peak, v)
        if peak > 0:
            dd = max(dd, (peak - v) / peak)
    return 100.0 * dd


def monthly_returns(points: Sequence[int], equity: Sequence[float]) -> list[dict]:
    """Calendar-month (UTC) returns. Month of a point = month of the instant just before it."""
    if len(points) < 2:
        return []
    months: list[dict] = []
    prev_val = equity[0]
    cur_key = None
    cur_end = None
    for p, v in zip(points[1:], equity[1:]):
        key = datetime.fromtimestamp(p - 1, UTC).strftime("%Y-%m")
        if cur_key is not None and key != cur_key:
            months.append({"month": cur_key, "return_pct": 100.0 * (cur_end / prev_val - 1.0) if prev_val > 0 else 0.0})
            prev_val = cur_end
        cur_key, cur_end = key, v
    months.append({"month": cur_key, "return_pct": 100.0 * (cur_end / prev_val - 1.0) if prev_val > 0 else 0.0})
    first = datetime.fromtimestamp(points[0], UTC)
    last = datetime.fromtimestamp(points[-1], UTC)
    first_partial = (first.day, first.hour, first.minute, first.second) != (1, 0, 0, 0)
    last_partial = (last.day, last.hour, last.minute, last.second) != (1, 0, 0, 0)
    for k, m in enumerate(months):
        m["partial"] = (k == 0 and first_partial) or (k == len(months) - 1 and last_partial)
    return months


def month_counts(months: Sequence[dict], eps: float = 1e-9) -> dict:
    pos = sum(1 for m in months if m["return_pct"] > eps)
    neg = sum(1 for m in months if m["return_pct"] < -eps)
    return {"positive": pos, "negative": neg, "flat": len(months) - pos - neg}


def series_metrics(points: Sequence[int], equity: Sequence[float], periods_per_year: float) -> dict:
    rets = daily_returns_from_equity(equity)
    months = monthly_returns(points, equity)
    start, end = equity[0], equity[-1]
    return {
        "start_equity": start,
        "end_equity": end,
        "pnl_usdt": end - start,
        "return_pct": 100.0 * (end / start - 1.0) if start > 0 else 0.0,
        "max_dd_pct": max_drawdown_pct(equity),
        "sharpe": sharpe(rets, periods_per_year),
        "months": months,
        "month_counts": month_counts(months),
    }


# ------------------------------------------------------------ buy and hold
def buy_and_hold(
    symbols: Sequence[str],
    candles: Callable[[str], list],
    points: Sequence[int],
    capital: float,
    *,
    fee_pct: float = 0.0,
    slippage_pct: Callable[[str], float] | None = None,
    bar_seconds: int = HOUR,
    max_entry_lag_s: int = DAY,
) -> dict:
    """Equal-weight buy and hold of ``symbols`` with ``capital`` over ``points``.

    Entry at the open of the first bar whose open is at or after the window
    start (the same causal convention as the engine: no bar that opened
    before the start is used). Every mark, including the first, is the
    liquidation value net of fee + slippage, as the engine marks open lots.
    """
    start, end = points[0], points[-1]
    legs = []
    excluded: dict[str, str] = {}
    for sym in symbols:
        bars = list(candles(sym) or [])
        ts = [int(b[0]) for b in bars]
        i = bisect.bisect_left(ts, start)
        if i >= len(bars) or ts[i] - start > max_entry_lag_s or ts[i] + bar_seconds > end:
            excluded[sym] = "no 1h bar at the window start" if i >= len(bars) or ts[i] - start > max_entry_lag_s else "no bar inside the window"
            continue
        legs.append((sym, bars, ts, i))
    if not legs:
        return {"symbols": [], "excluded": excluded, "equity": None}
    per_leg = capital / len(legs)
    fee = fee_pct / 100.0
    curves = []
    per_symbol = {}
    for sym, bars, ts, i in legs:
        slip = (slippage_pct(sym) if slippage_pct else 0.0) / 100.0
        entry = float(bars[i][1])
        qty = per_leg * (1.0 - fee) / (entry * (1.0 + slip))
        vals = []
        for p in points:
            # last bar that closed at or before p (bar open + bar_seconds <= p)
            j = bisect.bisect_right(ts, p - bar_seconds) - 1
            px = float(bars[j][4]) if j >= i else entry
            vals.append(qty * px * (1.0 - slip) * (1.0 - fee))
        curves.append(vals)
        per_symbol[sym] = {"entry_open": entry, "end_close": vals[-1] / (qty * (1.0 - slip) * (1.0 - fee)),
                           "return_pct": 100.0 * (vals[-1] / per_leg - 1.0)}
    equity = [sum(c[k] for c in curves) for k in range(len(points))]
    return {"symbols": [leg[0] for leg in legs], "excluded": excluded, "equity": equity, "per_symbol": per_symbol}


# ------------------------------------------------------------ entry timing
def entry_timing(lots: Iterable, start_ts: int, end_ts: int, local_tz: str) -> dict:
    """Entries inside [start, end) by hour and weekday, UTC and local time."""
    tz = ZoneInfo(local_tz)
    zones = {"UTC": UTC, local_tz: tz}
    out = {z: {"hour": {h: _bucket() for h in range(24)}, "weekday": {d: _bucket() for d in WEEKDAYS}} for z in zones}
    n = 0
    for lot in lots:
        et = lot.entry_ts
        if et.tzinfo is None:  # harness fills are UTC-aware; be strict if not
            raise ValueError("entry_ts must be timezone-aware")
        u = int(et.timestamp())
        if not (start_ts <= u < end_ts):
            continue
        n += 1
        for z, zi in zones.items():
            loc = et.astimezone(zi)
            for b in (out[z]["hour"][loc.hour], out[z]["weekday"][WEEKDAYS[loc.weekday()]]):
                b["n"] += 1
                b["pnl_usdt"] += lot.pnl
                b["wins"] += 1 if lot.pnl > 0 else 0
                if lot.ret is not None:
                    b["_ret"].append(100.0 * lot.ret)
    for z in out.values():
        for part in z.values():
            for b in part.values():
                r = b.pop("_ret")
                b["avg_ret_pct"] = statistics.fmean(r) if r else None
                b["hit_pct"] = 100.0 * b["wins"] / b["n"] if b["n"] else None
    return {"entries": n, "zones": list(zones), "by_zone": out}


def _bucket() -> dict:
    return {"n": 0, "wins": 0, "pnl_usdt": 0.0, "_ret": []}


# ------------------------------------------------------------------- report
def traded_symbols(run) -> list[str]:
    return sorted({lot.key[1] for lot in run.lots if lot.invested > 0})


def benchmark_report(
    run,
    candles: Callable[[str], list],
    start: datetime,
    end: datetime,
    *,
    config: BenchmarkConfig | None = None,
    fee_pct: float = 0.0,
    slippage_pct: Callable[[str], float] | None = None,
) -> dict:
    """Strategy vs Buy & Hold for one ``RunResult`` over [start, end]."""
    cfg = config or BenchmarkConfig()
    s, e = int(start.timestamp()), int(end.timestamp())
    points = sample_points(s, e)
    capital = cfg.capital if cfg.capital is not None else peak_deployed(run.capital_curve)
    rep = {
        "window_utc": [start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat()],
        "capital_base_usdt": capital,
        "capital_base_rule": "fixed" if cfg.capital is not None else "peak deployed capital",
        "sharpe_periods_per_year": cfg.periods_per_year,
        "strategy": None,
        "benchmarks": {},
        "entry_timing": entry_timing(run.lots, s, e, cfg.local_tz),
    }
    if capital <= 0 or len(points) < 2:
        rep["note"] = "no capital deployed or empty window: no benchmark"
        return rep
    strat = series_metrics(points, strategy_equity(run.pnl_curve, points, capital), cfg.periods_per_year)
    rep["strategy"] = strat
    for name, syms in cfg.baskets(traded_symbols(run)).items():
        bh = buy_and_hold(syms, candles, points, capital, fee_pct=fee_pct, slippage_pct=slippage_pct,
                          bar_seconds=cfg.bar_seconds, max_entry_lag_s=cfg.max_entry_lag_s)
        if bh["equity"] is None:
            rep["benchmarks"][name] = {"requested": len(syms), "symbols": [], "excluded": bh["excluded"], "metrics": None}
            continue
        m = series_metrics(points, bh["equity"], cfg.periods_per_year)
        rep["benchmarks"][name] = {
            "requested": len(syms),
            "symbols": bh["symbols"],
            "excluded": bh["excluded"],
            "per_symbol": bh["per_symbol"],
            "metrics": m,
            "excess_return_pp": strat["return_pct"] - m["return_pct"],
            "beats_return": strat["return_pct"] > m["return_pct"],
            "beats_sharpe": (strat["sharpe"] is not None and m["sharpe"] is not None and strat["sharpe"] > m["sharpe"]),
            "lower_dd": strat["max_dd_pct"] < m["max_dd_pct"],
        }
    return rep


def _f(x, d: int = 2) -> str:
    return "n/a" if x is None else f"{x:,.{d}f}"


def render_markdown(name: str, rep: dict, *, timing: bool = True, per_symbol_top: int = 5) -> list[str]:
    """Markdown lines for one run's benchmark report."""
    lines = [f"#### {name}: strategy vs Buy & Hold", ""]
    if rep.get("strategy") is None:
        return lines + [rep.get("note", "no benchmark"), ""]
    w0, w1 = rep["window_utc"]
    lines += [
        f"Window {w0[:16]} to {w1[:16]} UTC. Capital base {_f(rep['capital_base_usdt'], 0)} USDT ({rep['capital_base_rule']}). "
        f"Sharpe = daily equity returns, annualised x sqrt({_f(rep['sharpe_periods_per_year'], 0)}).",
        "",
        "| Series | Symbols | Return % | PnL USDT | Max DD % | Sharpe | Months +/- (flat) | Excess return pp | Beats on return | Beats on Sharpe | Lower max DD |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    s = rep["strategy"]
    mc = s["month_counts"]
    lines.append(f"| strategy | - | {_f(s['return_pct'])} | {_f(s['pnl_usdt'], 0)} | {_f(s['max_dd_pct'])} | {_f(s['sharpe'])} | "
                 f"{mc['positive']}/{mc['negative']} ({mc['flat']}) | - | - | - | - |")
    for bname, b in rep["benchmarks"].items():
        m = b["metrics"]
        if m is None:
            lines.append(f"| B&H {bname} | 0/{b['requested']} | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |")
            continue
        bc = m["month_counts"]
        lines.append(
            f"| B&H {bname} | {len(b['symbols'])}/{b['requested']} | {_f(m['return_pct'])} | {_f(m['pnl_usdt'], 0)} | {_f(m['max_dd_pct'])} | "
            f"{_f(m['sharpe'])} | {bc['positive']}/{bc['negative']} ({bc['flat']}) | {_f(b['excess_return_pp'])} | "
            f"{'yes' if b['beats_return'] else 'NO'} | {'yes' if b['beats_sharpe'] else 'NO'} | {'yes' if b['lower_dd'] else 'NO'} |"
        )
    lines.append("")
    months = [m["month"] for m in s["months"]]
    if months:
        lines += ["| Month (UTC) | strategy % | " + " | ".join(f"B&H {n} %" for n in rep["benchmarks"]) + " |",
                  "|---|---|" + "---|" * len(rep["benchmarks"])]
        for k, mo in enumerate(s["months"]):
            cells = []
            for b in rep["benchmarks"].values():
                bm = b["metrics"]["months"] if b["metrics"] else []
                cells.append(_f(bm[k]["return_pct"]) if k < len(bm) else "n/a")
            lines.append(f"| {mo['month']}{' (partial)' if mo['partial'] else ''} | {_f(mo['return_pct'])} | " + " | ".join(cells) + " |")
        lines.append("")
    for bname, b in rep["benchmarks"].items():
        if b.get("excluded"):
            lines.append(f"- B&H {bname}: excluded {len(b['excluded'])} symbol(s) without a usable price: "
                         + ", ".join(f"{k} ({v})" for k, v in sorted(b["excluded"].items())))
        ps = b.get("per_symbol") or {}
        if len(ps) > 1 and per_symbol_top:
            ranked = sorted(ps.items(), key=lambda kv: kv[1]["return_pct"])
            med = statistics.median(v["return_pct"] for v in ps.values())
            lines.append(f"- B&H {bname} per symbol: median {_f(med)} %; worst "
                         + ", ".join(f"{k} {_f(v['return_pct'])} %" for k, v in ranked[:per_symbol_top])
                         + "; best " + ", ".join(f"{k} {_f(v['return_pct'])} %" for k, v in ranked[-per_symbol_top:][::-1]))
        elif len(ps) == 1:
            (k, v), = ps.items()
            lines.append(f"- B&H {bname}: {k} open {_f(v['entry_open'], 4)} -> close {_f(v['end_close'], 4)}")
    lines.append("")
    if timing:
        lines += render_timing(rep["entry_timing"])
    return lines


def render_timing(et: dict) -> list[str]:
    zones = et["zones"]
    lines = [f"Entry timing ({et['entries']} entries in the window). n = entries, avg = mean lot return %, PnL = USDT; "
             "a win is PnL > 0.", ""]
    lines += ["| Hour | " + " | ".join(f"{z}: n / avg % / PnL" for z in zones) + " |", "|---|" + "---|" * len(zones)]
    for h in range(24):
        cells = []
        for z in zones:
            b = et["by_zone"][z]["hour"][h]
            cells.append(f"{b['n']} / {_f(b['avg_ret_pct'], 1)} / {_f(b['pnl_usdt'], 0)}")
        lines.append(f"| {h:02d} | " + " | ".join(cells) + " |")
    lines += ["", "| Weekday | " + " | ".join(f"{z}: n / avg % / PnL" for z in zones) + " |", "|---|" + "---|" * len(zones)]
    for d in WEEKDAYS:
        cells = []
        for z in zones:
            b = et["by_zone"][z]["weekday"][d]
            cells.append(f"{b['n']} / {_f(b['avg_ret_pct'], 1)} / {_f(b['pnl_usdt'], 0)}")
        lines.append(f"| {d} | " + " | ".join(cells) + " |")
    return lines + [""]
