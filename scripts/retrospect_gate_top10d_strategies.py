#!/usr/bin/env python3
"""10-day Gate top-gainer retrospective + strategy mix counterfactuals.

Goal (operator): IDENTIFY top coins and SELL profitably — not buy at the peak.

For each UTC day in the last N days:
  1) Rank liquid Gate USDT spot by that day's return (close/prev_close)
  2) For top-K: simulate entry mixes + exit mixes on 1h OHLCV
  3) Report which mixes would have captured profit

No orders. Public market data only.

  python3.13 scripts/retrospect_gate_top10d_strategies.py --days 10 --top 8
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ccxt  # noqa: E402

from historical_prices import _fetch_ohlcv_range  # noqa: E402

_STABLES = {
    "USDT", "USDC", "USD", "DAI", "BUSD", "FDUSD", "TUSD", "USDD", "USDE",
    "EUR", "EURT", "PYUSD",
}
_LEV = ("3L", "3S", "5L", "5S", "UP", "DOWN", "BULL", "BEAR")
FEE_RT = 0.002  # ~0.1% * 2


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def tradeable(sym: str) -> bool:
    if not sym or not str(sym).endswith("/USDT") or ":" in str(sym):
        return False
    base = str(sym).split("/")[0].upper()
    if base in _STABLES:
        return False
    return not any(base.endswith(s) for s in _LEV)


def liquid_symbols(min_vol: float, limit: int) -> list[str]:
    ex = ccxt.gate({"enableRateLimit": True, "options": {"defaultType": "spot"}})
    tickers = ex.fetch_tickers() or {}
    rows: list[tuple[str, float]] = []
    for sym, t in tickers.items():
        if not tradeable(sym) or not isinstance(t, dict):
            continue
        qv = t.get("quoteVolume")
        if qv is None:
            last = float(t.get("last") or 0)
            bv = float(t.get("baseVolume") or 0)
            qv = last * bv if last > 0 else 0.0
        qv = float(qv or 0)
        if qv < min_vol:
            continue
        rows.append((sym, qv))
    rows.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in rows[: max(1, limit)]]


def fetch_1d(symbols: list[str], start: datetime, end: datetime, workers: int) -> dict[str, list]:
    out: dict[str, list] = {}
    fs = start - timedelta(days=3)

    def one(sym: str) -> tuple[str, list]:
        try:
            return sym, _fetch_ohlcv_range(sym, fs, end, timeframe="1d") or []
        except Exception:
            return sym, []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(one, s): s for s in symbols}
        n = 0
        for fut in as_completed(futs):
            sym, bars = fut.result()
            out[sym] = bars
            n += 1
            if n % 30 == 0 or n == len(symbols):
                print(f"  1d {n}/{len(symbols)}", flush=True)
    return out


def fetch_1h_batch(symbols: list[str], start: datetime, end: datetime, workers: int) -> dict[str, list]:
    out: dict[str, list] = {}
    fs = start - timedelta(days=2)

    def one(sym: str) -> tuple[str, list]:
        try:
            return sym, _fetch_ohlcv_range(sym, fs, end, timeframe="1h") or []
        except Exception:
            return sym, []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(one, s): s for s in symbols}
        n = 0
        for fut in as_completed(futs):
            sym, bars = fut.result()
            out[sym] = bars
            n += 1
            if n % 15 == 0 or n == len(symbols):
                print(f"  1h {n}/{len(symbols)}", flush=True)
    return out


def day_returns(daily: dict[str, list], start: datetime, end: datetime) -> dict[str, list[dict]]:
    start_d = start.date()
    end_d = end.date()
    by_day: dict[str, list[dict]] = {}
    for sym, bars in daily.items():
        if not bars:
            continue
        bars = sorted(bars, key=lambda b: int(b[0]))
        for i, b in enumerate(bars):
            d = datetime.fromtimestamp(int(b[0]) / 1000, tz=timezone.utc).date()
            if d < start_d or d > end_d:
                continue
            o, h, l, c, v = map(float, b[1:6])
            if c <= 0:
                continue
            if i > 0 and float(bars[i - 1][4]) > 0:
                prev = float(bars[i - 1][4])
                ret = (c / prev - 1.0) * 100.0
                prev_c, prev_h = prev, float(bars[i - 1][2])
            elif o > 0:
                ret = (c / o - 1.0) * 100.0
                prev_c, prev_h = o, h
            else:
                continue
            by_day.setdefault(d.isoformat(), []).append(
                {
                    "symbol": sym,
                    "day_ret_pct": round(ret, 2),
                    "open": o,
                    "high": h,
                    "low": l,
                    "close": c,
                    "prev_close": prev_c,
                    "prev_high": prev_h,
                    "volume": v,
                    "day": d.isoformat(),
                }
            )
    for k in by_day:
        by_day[k].sort(key=lambda r: r["day_ret_pct"], reverse=True)
    return by_day


def bars_on_day(h1: list, day: str) -> list:
    d0 = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    d1 = d0 + timedelta(days=1)
    t0, t1 = int(d0.timestamp() * 1000), int(d1.timestamp() * 1000)
    return [b for b in sorted(h1 or [], key=lambda x: int(x[0])) if t0 <= int(b[0]) < t1]


def bars_from(h1: list, day: str, hours: int = 48) -> list:
    d0 = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    t0 = int(d0.timestamp() * 1000)
    t1 = t0 + hours * 3_600_000
    return [b for b in sorted(h1 or [], key=lambda x: int(x[0])) if t0 <= int(b[0]) < t1]


def find_entry(
    day_bars: list,
    *,
    mode: str,
    day_open: float,
    prev_close: float,
    prev_high: float,
) -> tuple[float, int] | None:
    """Return (entry_price, bar_index) or None."""
    if not day_bars or day_open <= 0:
        return None
    if mode == "open_of_day":
        return float(day_bars[0][1]), 0  # open of first hour
    if mode == "prev_close_proxy":
        # enter at first hour open ≈ day open after holding from prev close
        return day_open, 0
    if mode == "breakout_prev_high":
        thr = prev_high * 1.01 if prev_high > 0 else day_open * 1.05
        for i, b in enumerate(day_bars):
            if float(b[2]) >= thr:  # high touches
                return thr, i
        return None
    if mode == "accel_plus8":
        for i, b in enumerate(day_bars):
            c = float(b[4])
            if (c / day_open - 1.0) * 100.0 >= 8.0:
                return c, i
        return None
    if mode == "accel_plus15":
        for i, b in enumerate(day_bars):
            c = float(b[4])
            if (c / day_open - 1.0) * 100.0 >= 15.0:
                return c, i
        return None
    if mode == "first_green_hour":
        for i, b in enumerate(day_bars):
            o, c = float(b[1]), float(b[4])
            if c > o * 1.01:
                return c, i
        return None
    if mode == "vwap_dip4h":
        # buy lowest close of first 4 hours if still above day open
        window = day_bars[:4]
        if not window:
            return None
        best_i = min(range(len(window)), key=lambda i: float(window[i][4]))
        px = float(window[best_i][4])
        if px >= day_open * 0.98:
            return px, best_i
        return None
    return None


def run_exit(
    bars: list,
    entry_i: int,
    entry_px: float,
    *,
    mode: str,
) -> dict[str, Any] | None:
    if entry_px <= 0 or entry_i >= len(bars):
        return None
    path = bars[entry_i:]
    if not path:
        return None
    peak = entry_px
    armed = False
    trail_pct = 8.0
    arm_pct = 8.0
    if mode == "trail_tight":
        trail_pct, arm_pct = 5.0, 6.0
    elif mode == "trail_mid":
        trail_pct, arm_pct = 8.0, 8.0
    elif mode == "trail_wide":
        trail_pct, arm_pct = 12.0, 12.0
    elif mode == "hold_eod":
        last = path[-1] if len(path) == 1 or True else path[min(len(path) - 1, 23)]
        # same-day: last bar of provided path if day-only; use close of last bar same calendar if possible
        px = float(path[min(len(path) - 1, max(0, 23 - entry_i if False else len(path) - 1))][4])
        # simpler: sell at close of last bar in path slice for day
        # caller passes day-only or multi-day
        px = float(path[-1][4])
        # for hold_eod with multi-day path, sell at end of first day portion
        d0 = datetime.fromtimestamp(int(path[0][0]) / 1000, tz=timezone.utc).date()
        day_path = [
            b
            for b in path
            if datetime.fromtimestamp(int(b[0]) / 1000, tz=timezone.utc).date() == d0
        ]
        if day_path:
            px = float(day_path[-1][4])
        ret = (px / entry_px - 1.0) * 100.0 - FEE_RT * 100
        return {
            "exit_mode": mode,
            "exit_px": px,
            "pnl_pct": round(ret, 3),
            "peak_pct": round((peak / entry_px - 1.0) * 100, 3),
            "bars_held": len(day_path) or 1,
            "reason": "eod",
        }
    elif mode == "tp15_sl8":
        for j, b in enumerate(path):
            h, l, c = float(b[2]), float(b[3]), float(b[4])
            peak = max(peak, h)
            if h >= entry_px * 1.15:
                px = entry_px * 1.15
                ret = (px / entry_px - 1.0) * 100.0 - FEE_RT * 100
                return {
                    "exit_mode": mode,
                    "exit_px": px,
                    "pnl_pct": round(ret, 3),
                    "peak_pct": round((peak / entry_px - 1.0) * 100, 3),
                    "bars_held": j + 1,
                    "reason": "tp15",
                }
            if l <= entry_px * 0.92:
                px = entry_px * 0.92
                ret = (px / entry_px - 1.0) * 100.0 - FEE_RT * 100
                return {
                    "exit_mode": mode,
                    "exit_px": px,
                    "pnl_pct": round(ret, 3),
                    "peak_pct": round((peak / entry_px - 1.0) * 100, 3),
                    "bars_held": j + 1,
                    "reason": "sl8",
                }
        px = float(path[-1][4])
        ret = (px / entry_px - 1.0) * 100.0 - FEE_RT * 100
        return {
            "exit_mode": mode,
            "exit_px": px,
            "pnl_pct": round(ret, 3),
            "peak_pct": round((peak / entry_px - 1.0) * 100, 3),
            "bars_held": len(path),
            "reason": "timeout",
        }
    elif mode == "sell_day_high_oracle":
        # hindsight upper bound on same-day path
        d0 = datetime.fromtimestamp(int(path[0][0]) / 1000, tz=timezone.utc).date()
        day_path = [
            b
            for b in path
            if datetime.fromtimestamp(int(b[0]) / 1000, tz=timezone.utc).date() == d0
        ]
        if not day_path:
            day_path = path[:1]
        hi = max(float(b[2]) for b in day_path)
        ret = (hi / entry_px - 1.0) * 100.0 - FEE_RT * 100
        return {
            "exit_mode": mode,
            "exit_px": hi,
            "pnl_pct": round(ret, 3),
            "peak_pct": round(ret + FEE_RT * 100, 3),
            "bars_held": len(day_path),
            "reason": "oracle_high",
        }

    # trail family
    for j, b in enumerate(path):
        h, l, c = float(b[2]), float(b[3]), float(b[4])
        peak = max(peak, h)
        gain = (peak / entry_px - 1.0) * 100.0
        if gain >= arm_pct:
            armed = True
        if armed:
            stop = peak * (1.0 - trail_pct / 100.0)
            if l <= stop:
                px = stop
                ret = (px / entry_px - 1.0) * 100.0 - FEE_RT * 100
                return {
                    "exit_mode": mode,
                    "exit_px": px,
                    "pnl_pct": round(ret, 3),
                    "peak_pct": round(gain, 3),
                    "bars_held": j + 1,
                    "reason": "trail",
                }
    px = float(path[-1][4])
    ret = (px / entry_px - 1.0) * 100.0 - FEE_RT * 100
    return {
        "exit_mode": mode,
        "exit_px": px,
        "pnl_pct": round(ret, 3),
        "peak_pct": round((peak / entry_px - 1.0) * 100, 3),
        "bars_held": len(path),
        "reason": "timeout",
    }


ENTRY_MODES = [
    "open_of_day",
    "prev_close_proxy",
    "breakout_prev_high",
    "accel_plus8",
    "accel_plus15",
    "first_green_hour",
    "vwap_dip4h",
]
EXIT_MODES = [
    "hold_eod",
    "trail_tight",
    "trail_mid",
    "trail_wide",
    "tp15_sl8",
    "sell_day_high_oracle",
]


def simulate_symbol_day(g: dict, h1: list) -> dict:
    day = g["day"]
    day_bars = bars_on_day(h1, day)
    multi = bars_from(h1, day, hours=36)
    day_open = float(g["open"])
    prev_c = float(g.get("prev_close") or day_open)
    prev_h = float(g.get("prev_high") or day_open)
    oracle_day = (float(g["high"]) / day_open - 1.0) * 100.0 if day_open > 0 else 0.0
    mixes: list[dict] = []
    for em in ENTRY_MODES:
        ent = find_entry(
            day_bars,
            mode=em,
            day_open=day_open,
            prev_close=prev_c,
            prev_high=prev_h,
        )
        if not ent:
            mixes.append({"entry": em, "filled": False})
            continue
        epx, ei = ent
        # map entry index into multi path
        if not multi:
            mixes.append({"entry": em, "filled": False})
            continue
        # find bar matching day_bars[ei] ts
        ts = int(day_bars[ei][0]) if ei < len(day_bars) else int(multi[0][0])
        mi = next((i for i, b in enumerate(multi) if int(b[0]) == ts), 0)
        row: dict[str, Any] = {
            "entry": em,
            "filled": True,
            "entry_px": epx,
            "entry_bar": ei,
            "exits": {},
        }
        for xm in EXIT_MODES:
            path = multi if xm.startswith("trail") or xm == "tp15_sl8" else day_bars
            # for trail use multi from entry
            if xm.startswith("trail") or xm == "tp15_sl8":
                ex = run_exit(multi, mi, epx, mode=xm)
            else:
                # day_bars from entry index
                ex = run_exit(day_bars, ei, epx, mode=xm)
            if ex:
                row["exits"][xm] = ex
        mixes.append(row)

    # best non-oracle mix by pnl
    best = None
    for m in mixes:
        if not m.get("filled"):
            continue
        for xm, ex in (m.get("exits") or {}).items():
            if xm == "sell_day_high_oracle":
                continue
            pnl = ex.get("pnl_pct")
            if pnl is None:
                continue
            if best is None or pnl > best["pnl_pct"]:
                best = {
                    "entry": m["entry"],
                    "exit": xm,
                    "pnl_pct": pnl,
                    "peak_pct": ex.get("peak_pct"),
                    "reason": ex.get("reason"),
                }

    return {
        "symbol": g["symbol"],
        "day": day,
        "rank": g.get("rank"),
        "day_ret_pct": g["day_ret_pct"],
        "oracle_open_to_high_pct": round(oracle_day, 2),
        "day_open": day_open,
        "day_high": g["high"],
        "day_close": g["close"],
        "mixes": mixes,
        "best_tradable": best,
    }


def aggregate_mixes(results: list[dict]) -> dict:
    # results = list of simulate_symbol_day
    stats: dict[str, dict[str, list[float]]] = {}
    fill_n: dict[str, int] = {}
    total = 0
    for r in results:
        total += 1
        for m in r.get("mixes") or []:
            em = m["entry"]
            fill_n[em] = fill_n.get(em, 0) + (1 if m.get("filled") else 0)
            if not m.get("filled"):
                continue
            for xm, ex in (m.get("exits") or {}).items():
                key = f"{em}__{xm}"
                stats.setdefault(key, {"pnls": []})["pnls"].append(float(ex["pnl_pct"]))

    summary = []
    for key, d in stats.items():
        pnls = d["pnls"]
        if not pnls:
            continue
        em, xm = key.split("__", 1)
        wins = sum(1 for p in pnls if p > 0)
        summary.append(
            {
                "entry": em,
                "exit": xm,
                "n": len(pnls),
                "fill_rate_entry": round(fill_n.get(em, 0) / max(total, 1), 3),
                "avg_pnl": round(mean(pnls), 3),
                "med_pnl": round(median(pnls), 3),
                "win_rate": round(wins / len(pnls), 3),
                "sum_pnl": round(sum(pnls), 2),
                "p05": round(sorted(pnls)[max(0, int(0.05 * len(pnls)) - 1)], 3),
                "p95": round(sorted(pnls)[min(len(pnls) - 1, int(0.95 * len(pnls)))], 3),
            }
        )
    summary.sort(key=lambda x: x["avg_pnl"], reverse=True)
    return {"n_symbol_days": total, "mixes": summary, "entry_fills": fill_n}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--scan", type=int, default=180)
    ap.add_argument("--min-vol", type=float, default=500_000)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--min-day-ret", type=float, default=5.0)
    args = ap.parse_args()

    end = _utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    # exclude incomplete today → last full days
    end = end  # today 00:00 UTC; last closed day is yesterday
    start = end - timedelta(days=args.days)

    print(f"Window UTC [{start.date()} .. {end.date() - timedelta(days=1)}] top={args.top}", flush=True)
    print("Fetching liquid Gate universe…", flush=True)
    syms = liquid_symbols(args.min_vol, args.scan)
    print(f"  liquid symbols: {len(syms)}", flush=True)

    print("Fetching 1d OHLCV…", flush=True)
    d1 = fetch_1d(syms, start, end, args.workers)
    by_day = day_returns(d1, start, end - timedelta(seconds=1))
    days = sorted(by_day.keys())
    # drop today if present
    today = _utc_now().date().isoformat()
    days = [d for d in days if d < today][-args.days :]

    # pick top-K per day
    picks: list[dict] = []
    for day in days:
        rows = [r for r in by_day.get(day, []) if r["day_ret_pct"] >= args.min_day_ret]
        for i, r in enumerate(rows[: args.top], 1):
            picks.append({**r, "rank": i})

    uniq = sorted({p["symbol"] for p in picks})
    print(f"Days={len(days)} picks={len(picks)} unique={len(uniq)}", flush=True)
    print("Fetching 1h OHLCV for picks…", flush=True)
    h1 = fetch_1h_batch(uniq, start - timedelta(days=1), end + timedelta(days=1), args.workers)

    per: list[dict] = []
    for p in picks:
        sim = simulate_symbol_day(p, h1.get(p["symbol"]) or [])
        per.append(sim)

    mix_sum = aggregate_mixes(per)

    # per-day narrative board
    daily_board = []
    for day in days:
        day_rows = [r for r in per if r["day"] == day]
        daily_board.append(
            {
                "day": day,
                "top": [
                    {
                        "rank": r["rank"],
                        "symbol": r["symbol"],
                        "day_ret_pct": r["day_ret_pct"],
                        "oracle_open_to_high_pct": r["oracle_open_to_high_pct"],
                        "best_tradable": r.get("best_tradable"),
                    }
                    for r in sorted(day_rows, key=lambda x: x["rank"] or 99)
                ],
            }
        )

    # strategy mix recommendation: top tradable mixes by avg pnl with n>= max(8, 30% of samples)
    min_n = max(8, int(0.25 * mix_sum["n_symbol_days"]))
    tradable = [
        m
        for m in mix_sum["mixes"]
        if m["exit"] != "sell_day_high_oracle" and m["n"] >= min_n
    ]
    top_mixes = tradable[:12]

    out = {
        "generated_at": _utc_now().isoformat(),
        "window": {"start": start.isoformat(), "end_exclusive": end.isoformat(), "days": days},
        "params": {
            "top_per_day": args.top,
            "scan": args.scan,
            "min_vol": args.min_vol,
            "min_day_ret": args.min_day_ret,
            "fee_rt": FEE_RT,
        },
        "daily_board": daily_board,
        "mix_summary": mix_sum,
        "recommended_mixes": top_mixes,
        "detail": per,
    }

    ts = _utc_now().strftime("%Y%m%d_%H%M%S")
    path = ROOT / "auswertungen" / f"gate_top10d_strategies_{ts}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")

    # print report
    print("\n" + "=" * 100)
    print("GATE TOP GAINERS — 10D RETROSPECTIVE (identify + sell, not peak FOMO)")
    print("=" * 100)
    for block in daily_board:
        print(f"\n--- {block['day']} ---")
        print(f"{'#':>2} {'symbol':14} {'day%':>7} {'o→hi%':>7}  best tradable mix")
        for r in block["top"]:
            b = r.get("best_tradable") or {}
            if b:
                bm = f"{b.get('entry')}+{b.get('exit')} → {b.get('pnl_pct'):+.1f}%"
            else:
                bm = "(no fill)"
            print(
                f"{r['rank']:2d} {r['symbol']:14} {r['day_ret_pct']:+7.1f} "
                f"{r['oracle_open_to_high_pct']:+7.1f}  {bm}"
            )

    print("\n" + "-" * 100)
    print(f"MIX LEADERBOARD (n>={min_n}, fees≈{FEE_RT*100:.1f}% rt, no oracle exits)")
    print(f"{'entry':22} {'exit':18} {'n':>4} {'avg%':>7} {'med%':>7} {'win':>6} {'sum%':>8}")
    for m in top_mixes[:15]:
        print(
            f"{m['entry']:22} {m['exit']:18} {m['n']:4d} {m['avg_pnl']:+7.2f} "
            f"{m['med_pnl']:+7.2f} {m['win_rate']*100:5.1f}% {m['sum_pnl']:+8.1f}"
        )

    print(f"\nWrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
