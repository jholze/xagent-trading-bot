#!/usr/bin/env python3
"""30d backtest: fixed_v0 vs coin_aware_v1 gainer entry policies.

Uses public Gate Spot data only (no ledger writes).

Method (honest-ish, documented look-ahead limits):
  - Universe: top liquid USDT spot by *current* quote volume (min 500k), excl leverage.
  - Each UTC day D: rank by previous calendar day return (close[D-1]/close[D-2]).
  - ATR%% from 1h bars ending at start of D (14-period ATR / close).
  - Board state scans/first_seen simulated across consecutive days.
  - Entry at open of day D (1h bar open); exits at +6h / +24h / +48h mid prices.
  - Caps: max 3 open; skip if already in open set; max 6 buys/day.
  - Fees: 0.2% round-trip.

  python3.13 scripts/backtest_gainer_entry_policies_30d.py --days 30
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ccxt  # noqa: E402

from historical_prices import _fetch_ohlcv_range  # noqa: E402
from services.gainer_signal.pure import (  # noqa: E402
    DEFAULT_ELIGIBLE_MIN_VOL,
    select_entry_signals,
)

_STABLES = {
    "USDT",
    "USDC",
    "USD",
    "DAI",
    "BUSD",
    "FDUSD",
    "TUSD",
    "USDD",
    "USDE",
    "EUR",
    "EURT",
    "PYUSD",
}
_LEV = ("3L", "3S", "5L", "5S", "UP", "DOWN", "BULL", "BEAR")
FEE_RT = 0.002


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def tradeable(sym: str) -> bool:
    if not sym or not str(sym).endswith("/USDT") or ":" in str(sym):
        return False
    base = str(sym).split("/")[0].upper()
    if base in _STABLES:
        return False
    return not any(base.endswith(s) for s in _LEV)


def liquid_symbols(min_vol: float, limit: int) -> list[tuple[str, float]]:
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
    return rows[: max(1, limit)]


def fetch_1h(symbols: list[str], start: datetime, end: datetime, workers: int) -> dict[str, list]:
    out: dict[str, list] = {}
    fs = start - timedelta(days=5)

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
            if n % 20 == 0 or n == len(symbols):
                print(f"  1h OHLCV {n}/{len(symbols)}", flush=True)
    return out


def atr_pct_at(bars_1h: list, asof: datetime, period: int = 14) -> float | None:
    """ATR% from 1h bars with bar.ts <= asof."""
    if not bars_1h:
        return None
    asof_ms = int(asof.timestamp() * 1000)
    bars = [b for b in bars_1h if int(b[0]) <= asof_ms]
    if len(bars) < period + 2:
        return None
    # Wilder-ish simple ATR on last `period` true ranges
    trs: list[float] = []
    for i in range(1, len(bars)):
        h, l, c_prev = float(bars[i][2]), float(bars[i][3]), float(bars[i - 1][4])
        tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
        trs.append(tr)
    if len(trs) < period:
        return None
    atr = mean(trs[-period:])
    close = float(bars[-1][4])
    if close <= 0:
        return None
    return float(atr / close * 100.0)


def price_at_or_after(bars_1h: list, when: datetime) -> float | None:
    ms = int(when.timestamp() * 1000)
    for b in bars_1h:
        if int(b[0]) >= ms:
            return float(b[1])  # open
    return None


def price_at_or_before(bars_1h: list, when: datetime) -> float | None:
    ms = int(when.timestamp() * 1000)
    last = None
    for b in bars_1h:
        if int(b[0]) <= ms:
            last = float(b[4])  # close
        else:
            break
    return last


def day_prev_return_leaders(
    bars_1h: dict[str, list],
    day: datetime,
    vol_proxy: dict[str, float],
    min_vol: float,
    top_n: int,
) -> list[dict[str, Any]]:
    """Rank by return of previous UTC calendar day (close D-1 / close D-2)."""
    d0 = day.date()
    d_prev = (day - timedelta(days=1)).date()
    d_prev2 = (day - timedelta(days=2)).date()
    rows: list[dict[str, Any]] = []
    for sym, bars in bars_1h.items():
        if not bars:
            continue
        by_day: dict = {}
        for b in bars:
            dd = datetime.fromtimestamp(int(b[0]) / 1000, tz=timezone.utc).date()
            by_day[dd] = b  # last hour close of that day wins
        b1 = by_day.get(d_prev)
        b2 = by_day.get(d_prev2)
        if not b1 or not b2:
            continue
        c1, c2 = float(b1[4]), float(b2[4])
        if c1 <= 0 or c2 <= 0:
            continue
        pct = (c1 / c2 - 1.0) * 100.0
        qv = float(vol_proxy.get(sym) or 0)
        # scale proxy: use current 24h vol as stand-in (limit of reconstruction)
        lev = False
        eligible = qv >= min_vol and not lev
        last = c1
        rows.append(
            {
                "symbol": sym,
                "pct_24h": round(pct, 4),
                "quote_vol": qv,
                "last": last,
                "leverage": lev,
                "eligible": eligible,
                "reject_reason": None if eligible else "low_volume",
            }
        )
    rows.sort(key=lambda r: (float(r["pct_24h"]), float(r["quote_vol"])), reverse=True)
    out = []
    for i, r in enumerate(rows[:top_n], 1):
        rr = dict(r)
        rr["rank"] = i
        out.append(rr)
    return out


def summarize_trades(trades: list[dict[str, Any]], horizon: str) -> dict[str, Any]:
    if not trades:
        return {
            "n": 0,
            "win_rate": None,
            "avg_pnl_pct": None,
            "median_pnl_pct": None,
            "sum_pnl_pct": 0.0,
            "median_entry_pct_24h": None,
            "pct_entry_over_40": None,
            "pct_entry_over_50": None,
        }
    pnls = [float(t[horizon]) for t in trades if t.get(horizon) is not None]
    entries = [float(t["entry_pct_24h"]) for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    return {
        "n": len(pnls),
        "win_rate": round(wins / len(pnls), 4) if pnls else None,
        "avg_pnl_pct": round(mean(pnls), 4) if pnls else None,
        "median_pnl_pct": round(median(pnls), 4) if pnls else None,
        "sum_pnl_pct": round(sum(pnls), 4) if pnls else 0.0,
        "median_entry_pct_24h": round(median(entries), 4) if entries else None,
        "pct_entry_over_40": round(
            sum(1 for e in entries if e > 40) / len(entries), 4
        )
        if entries
        else None,
        "pct_entry_over_50": round(
            sum(1 for e in entries if e > 50) / len(entries), 4
        )
        if entries
        else None,
    }


def run_policy(
    *,
    policy: str,
    days: list[datetime],
    bars_1h: dict[str, list],
    vol_proxy: dict[str, float],
    min_vol: float,
    top_n: int,
    max_rank: int,
    max_open: int,
    max_buys_day: int,
) -> dict[str, Any]:
    symbol_state: dict[str, dict[str, Any]] = {}
    prev_board: dict[str, dict[str, Any]] = {}
    open_slots: list[dict[str, Any]] = []  # {symbol, entry_day, exit_after}
    trades: list[dict[str, Any]] = []
    daily_log: list[dict[str, Any]] = []
    buys_today = 0
    last_day_key = ""

    for day in days:
        day_key = day.date().isoformat()
        if day_key != last_day_key:
            buys_today = 0
            last_day_key = day_key

        # free slots that expired
        open_slots = [s for s in open_slots if s["exit_day"] > day.date()]

        leaders = day_prev_return_leaders(
            bars_1h, day, vol_proxy, min_vol=min_vol, top_n=top_n
        )
        # update board state for top max_rank
        top_syms = set()
        now_ts = day.timestamp()
        for r in leaders:
            rank = int(r["rank"])
            sym = r["symbol"]
            if rank > max_rank:
                continue
            top_syms.add(sym)
            st = symbol_state.get(sym) or {}
            if "first_seen_top_k_at" not in st:
                st["first_seen_top_k_at"] = now_ts
            st["scans_in_top_k"] = int(st.get("scans_in_top_k") or 0) + 1
            st["prev_rank"] = st.get("rank")
            st["rank"] = rank
            symbol_state[sym] = st
            r["scans_in_top_k"] = st["scans_in_top_k"]
            r["first_seen_top_k_at"] = st["first_seen_top_k_at"]
        for sym in list(symbol_state.keys()):
            if sym not in top_syms:
                del symbol_state[sym]

        atr_map: dict[str, float] = {}
        for r in leaders:
            if int(r["rank"]) > max_rank or not r.get("eligible"):
                continue
            sym = r["symbol"]
            a = atr_pct_at(bars_1h.get(sym) or [], day)
            if a is not None:
                atr_map[sym] = a

        sigs = select_entry_signals(
            leaders,
            entry_policy=policy,
            max_rank=max_rank,
            prev_board=prev_board,
            symbol_state=symbol_state,
            atr_by_symbol=atr_map,
            now_ts=now_ts,
            hard_ceiling=50.0,
            heat_min=12.0,
            heat_max=40.0,
        )
        prev_board = {r["symbol"]: r for r in leaders}

        n_sig = 0
        open_syms = {s["symbol"] for s in open_slots}
        for sig in sigs:
            if buys_today >= max_buys_day:
                break
            if len(open_slots) >= max_open:
                break
            sym = sig["symbol"]
            if sym in open_syms:
                continue
            entry = price_at_or_after(bars_1h.get(sym) or [], day)
            if not entry or entry <= 0:
                continue
            # forward horizons
            p6 = price_at_or_before(bars_1h.get(sym) or [], day + timedelta(hours=6))
            p24 = price_at_or_before(bars_1h.get(sym) or [], day + timedelta(hours=24))
            p48 = price_at_or_before(bars_1h.get(sym) or [], day + timedelta(hours=48))

            def pnl(px: float | None) -> float | None:
                if px is None or px <= 0:
                    return None
                return round((px / entry - 1.0) * 100.0 - FEE_RT * 100.0, 4)

            tr = {
                "day": day_key,
                "symbol": sym,
                "entry_pct_24h": sig.get("pct_24h"),
                "rank": sig.get("rank"),
                "trigger": sig.get("trigger"),
                "vol_bucket": sig.get("vol_bucket"),
                "atr_pct": sig.get("atr_pct"),
                "entry_policy": policy,
                "entry_price": entry,
                "pnl_6h": pnl(p6),
                "pnl_24h": pnl(p24),
                "pnl_48h": pnl(p48),
            }
            trades.append(tr)
            open_slots.append(
                {
                    "symbol": sym,
                    "exit_day": (day + timedelta(days=2)).date(),
                }
            )
            open_syms.add(sym)
            buys_today += 1
            n_sig += 1

        daily_log.append(
            {
                "day": day_key,
                "n_leaders": len(leaders),
                "n_eligible_top": sum(1 for r in leaders if r.get("eligible")),
                "n_signals": len(sigs),
                "n_fills": n_sig,
                "open_after": len(open_slots),
            }
        )

    return {
        "policy": policy,
        "n_trades": len(trades),
        "summary_6h": summarize_trades(trades, "pnl_6h"),
        "summary_24h": summarize_trades(trades, "pnl_24h"),
        "summary_48h": summarize_trades(trades, "pnl_48h"),
        "trades": trades,
        "daily": daily_log,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--universe", type=int, default=80)
    ap.add_argument("--min-vol", type=float, default=DEFAULT_ELIGIBLE_MIN_VOL)
    ap.add_argument("--top-n", type=int, default=100)
    ap.add_argument("--max-rank", type=int, default=20)
    ap.add_argument("--max-open", type=int, default=3)
    ap.add_argument("--max-buys-day", type=int, default=6)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument(
        "--out-dir",
        type=str,
        default=str(ROOT / "auswertungen" / "gis"),
    )
    args = ap.parse_args()

    end = utc_now().replace(minute=0, second=0, microsecond=0)
    # decision days: last N full UTC days ending yesterday (need forward 48h)
    last_decision = (end - timedelta(days=2)).replace(hour=0)
    days = [
        last_decision - timedelta(days=i)
        for i in range(args.days - 1, -1, -1)
    ]
    start_fetch = days[0] - timedelta(days=5)
    end_fetch = end

    print("=== Gainer entry policy backtest ===", flush=True)
    print(f"days={args.days} decisions {days[0].date()} .. {days[-1].date()}", flush=True)
    print("Loading liquid universe…", flush=True)
    uni = liquid_symbols(args.min_vol, args.universe)
    symbols = [s for s, _ in uni]
    vol_proxy = {s: q for s, q in uni}
    print(f"universe n={len(symbols)} min_vol={args.min_vol}", flush=True)

    print("Fetching 1h OHLCV…", flush=True)
    bars = fetch_1h(symbols, start_fetch, end_fetch, workers=args.workers)

    results = {}
    for pol in ("fixed_v0", "coin_aware_v1"):
        print(f"Simulating policy={pol}…", flush=True)
        results[pol] = run_policy(
            policy=pol,
            days=days,
            bars_1h=bars,
            vol_proxy=vol_proxy,
            min_vol=args.min_vol,
            top_n=args.top_n,
            max_rank=args.max_rank,
            max_open=args.max_open,
            max_buys_day=args.max_buys_day,
        )
        s = results[pol]["summary_24h"]
        print(
            f"  {pol}: trades={s['n']} win={s['win_rate']} "
            f"med_pnl_24h={s['median_pnl_pct']} med_entry%={s['median_entry_pct_24h']} "
            f">40%_entries={s['pct_entry_over_40']}",
            flush=True,
        )

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().strftime("%Y-%m-%d")
    payload = {
        "generated_at": utc_now().isoformat(),
        "method": {
            "days": args.days,
            "universe": args.universe,
            "min_vol": args.min_vol,
            "rank_signal": "prev_calendar_day_return",
            "entry": "open_of_decision_day_1h",
            "horizons_h": [6, 24, 48],
            "fee_rt_pct": FEE_RT * 100,
            "caps": {"max_open": args.max_open, "max_buys_day": args.max_buys_day},
            "limitations": [
                "Universe from *current* liquid set (survivorship / listing bias).",
                "quote_vol proxy is current 24h vol, not historical day vol.",
                "Decision uses previous full-day return, not intraday WS board.",
                "Exit is fixed horizon, not trail/exit-radar.",
                "Does not model dual gainer_universe balloon path.",
            ],
        },
        "comparison": {
            "fixed_v0": {
                k: results["fixed_v0"][k]
                for k in (
                    "n_trades",
                    "summary_6h",
                    "summary_24h",
                    "summary_48h",
                )
            },
            "coin_aware_v1": {
                k: results["coin_aware_v1"][k]
                for k in (
                    "n_trades",
                    "summary_6h",
                    "summary_24h",
                    "summary_48h",
                )
            },
        },
        "results": results,
    }
    json_path = out_dir / f"{stamp}_gainer_entry_policy_30d.json"
    md_path = out_dir / f"{stamp}_gainer_entry_policy_30d.md"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    f0 = results["fixed_v0"]["summary_24h"]
    c1 = results["coin_aware_v1"]["summary_24h"]
    md = f"""# Gainer entry policy backtest ({args.days}d)

Generated: `{payload['generated_at']}`

## Method (read limitations)

- Rank: **previous UTC day** return on liquid Gate USDT spot
- Entry: open of decision day; exits **+6h / +24h / +48h**; fee 0.2% RT
- Caps: max {args.max_open} open, max {args.max_buys_day}/day
- Universe n={len(symbols)}, min_vol={args.min_vol}

**Limitations:** current liquid universe (survivorship); current vol proxy; no WS intraday; fixed horizon ≠ trail; no legacy balloon.

## 24h horizon comparison

| Policy | Trades | Win rate | Median PnL % | Avg PnL % | Sum PnL % | Median entry 24h% | Entries >40% | Entries >50% |
|--------|--------|----------|--------------|-----------|-----------|-------------------|--------------|--------------|
| fixed_v0 | {f0['n']} | {f0['win_rate']} | {f0['median_pnl_pct']} | {f0['avg_pnl_pct']} | {f0['sum_pnl_pct']} | {f0['median_entry_pct_24h']} | {f0['pct_entry_over_40']} | {f0['pct_entry_over_50']} |
| coin_aware_v1 | {c1['n']} | {c1['win_rate']} | {c1['median_pnl_pct']} | {c1['avg_pnl_pct']} | {c1['sum_pnl_pct']} | {c1['median_entry_pct_24h']} | {c1['pct_entry_over_40']} | {c1['pct_entry_over_50']} |

## 6h / 48h

### fixed_v0
- 6h: `{json.dumps(results['fixed_v0']['summary_6h'])}`
- 48h: `{json.dumps(results['fixed_v0']['summary_48h'])}`

### coin_aware_v1
- 6h: `{json.dumps(results['coin_aware_v1']['summary_6h'])}`
- 48h: `{json.dumps(results['coin_aware_v1']['summary_48h'])}`

## Validity read

- Prefer **coin_aware** if: lower median entry extension AND not much worse (or better) median/avg 24h PnL, and enough trades (n not ~0).
- Prefer **fixed_v0** if: coin_aware starves (n very low) or worse expectancy with no FOMO reduction.
- Neither is live truth without trail exits + dual stack.

Full JSON: `{json_path.name}`
"""
    md_path.write_text(md, encoding="utf-8")
    print(f"Wrote {json_path}", flush=True)
    print(f"Wrote {md_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
