#!/usr/bin/env python3
"""Renew personal entry params for watchlist ∪ open ∪ past trade symbols.

Primary 30d metric (pre-declared): mean_total_return_pct of chosen recipe path
vs tier-default baseline on the same OHLCV window (see strategies.entry_recipe).

Usage:
  DEMO_MODE=1 python3 scripts/renew_entry_recipes.py --days 30 --max-symbols 40
  DEMO_MODE=1 python3 scripts/renew_entry_recipes.py --synthetic --symbols BTC/USDT,ETH/USDT
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DEMO_MODE", "1")


def _load_universe_from_local(args: argparse.Namespace) -> list[str]:
    from strategies.entry_recipe import build_symbol_universe, normalize_symbol

    if args.symbols:
        return [normalize_symbol(s) for s in args.symbols.split(",") if s.strip()]

    watchlist: list[Any] = []
    positions: list[Any] = []
    orders: list[Any] = []

    # config watchlist
    try:
        cfg_path = ROOT / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        for c in cfg.get("watchlist") or cfg.get("coins") or []:
            if isinstance(c, dict):
                watchlist.append(c)
            elif isinstance(c, str):
                watchlist.append({"symbol": c})
        for entry in cfg.get("strategies") or []:
            if entry.get("symbol"):
                watchlist.append(entry)
    except Exception as e:
        print(f"watchlist load warn: {e}", file=sys.stderr)

    # local demo ledger json if present
    for name in ("orders.demo.json", "orders.json", "data/orders.demo.json"):
        p = ROOT / name
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            orders.extend(data.get("orders") or [])
        except Exception:
            pass

    for name in ("positions.demo.json", "positions.json"):
        p = ROOT / name
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            posmap = data.get("positions") or {}
            for k, v in posmap.items():
                if isinstance(v, dict):
                    sym = v.get("symbol")
                    if not sym and "_" in k:
                        # BEAT_USDT_4h
                        parts = k.rsplit("_", 1)[0].replace("_", "/")
                        if parts.count("/") == 0 and parts.endswith("USDT"):
                            parts = parts[:-4] + "/USDT"
                        sym = parts
                    positions.append({"symbol": sym, **v})
        except Exception:
            pass

    # optional mongo when available
    if args.mongo and os.environ.get("MONGO_URL"):
        try:
            from pymongo import MongoClient

            dbn = os.environ.get("MONGODB_DB", "xagent_test")
            db = MongoClient(os.environ["MONGO_URL"], serverSelectionTimeoutMS=4000)[dbn]
            for tid in ("default", "henry"):
                blob = db["orders"].find_one({"_id": f"{tid}:demo"}) or {}
                orders.extend(blob.get("orders") or [])
                pos = db["positions"].find_one({"_id": f"{tid}:demo"}) or {}
                for k, v in (pos.get("positions") or {}).items():
                    if isinstance(v, dict) and float(v.get("amount") or 0) > 0:
                        base = k.rsplit("_", 1)[0].replace("_", "/")
                        if not base.endswith("/USDT") and base.endswith("USDT"):
                            base = base[:-4] + "/USDT"
                        positions.append({"symbol": v.get("symbol") or base})
        except Exception as e:
            print(f"mongo universe warn: {e}", file=sys.stderr)

    # always include liquid majors so 30d proof has data
    defaults = [
        "BTC/USDT",
        "ETH/USDT",
        "SOL/USDT",
        "XRP/USDT",
        "DOGE/USDT",
        "ADA/USDT",
        "AVAX/USDT",
        "LINK/USDT",
        "NEAR/USDT",
        "DOT/USDT",
    ]
    watchlist.extend({"symbol": s} for s in defaults)

    symbols = build_symbol_universe(
        watchlist=watchlist, open_positions=positions, orders=orders
    )
    if args.max_symbols and len(symbols) > args.max_symbols:
        symbols = symbols[: args.max_symbols]
    return symbols


def _synthetic_ohlcv(days: int = 30, seed: int = 42) -> "pd.DataFrame":
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(seed)
    n = max(days * 24, 80)
    # mean-reverting series so dip/RSI entries can fire
    rets = rng.normal(0.0, 0.008, size=n)
    price = 100 * np.exp(np.cumsum(rets))
    # inject a few dips
    for i in range(30, n, 40):
        price[i : i + 5] *= 0.96
    vol = rng.uniform(1e3, 5e3, size=n)
    vol[::17] *= 3.0
    ts = pd.date_range(end=datetime.now(timezone.utc), periods=n, freq="1h")
    return pd.DataFrame(
        {
            "ts": (ts.view("int64") // 10**6),
            "open": price,
            "high": price * 1.01,
            "low": price * 0.99,
            "close": price,
            "volume": vol,
        }
    )


def _fetch_ohlcv(symbol: str, timeframe: str, days: int):
    import pandas as pd

    from historical_prices import _fetch_ohlcv_range

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days + 2)
    try:
        bars = _fetch_ohlcv_range(symbol, start, end, timeframe=timeframe)
    except Exception as e:
        print(f"fetch fail {symbol}: {e}", file=sys.stderr)
        return None
    if not bars or len(bars) < 40:
        return None
    df = pd.DataFrame(bars, columns=["ts", "open", "high", "low", "close", "volume"])
    return df


def main() -> int:
    parser = argparse.ArgumentParser(description="Renew personal entry recipes")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--max-symbols", type=int, default=30)
    parser.add_argument("--symbols", default="")
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--mongo", action="store_true")
    parser.add_argument("--no-persist", action="store_true")
    parser.add_argument("--out", default="")
    parser.add_argument("--tier", default="volatile")
    args = parser.parse_args()

    from strategies.entry_recipe import (
        PRIMARY_METRIC,
        add_indicators,
        compare_cohort,
        renew_symbol_params,
    )

    symbols = _load_universe_from_local(args)
    print(f"universe n={len(symbols)} symbols={symbols[:15]}{'...' if len(symbols)>15 else ''}")

    results = []
    details = []
    for i, sym in enumerate(symbols):
        if args.synthetic:
            df = _synthetic_ohlcv(args.days, seed=1000 + i)
        else:
            df = _fetch_ohlcv(sym, args.timeframe, args.days)
            if df is None:
                df = _synthetic_ohlcv(args.days, seed=1000 + i)
                fb_data = "synthetic_ohlcv"
            else:
                fb_data = ""
        df = add_indicators(df)
        rr = renew_symbol_params(
            sym,
            df,
            timeframe=args.timeframe,
            tier=args.tier,
            persist=not args.no_persist,
        )
        if not args.synthetic and "fb_data" in dir():
            pass
        results.append(rr)
        details.append(
            {
                "symbol": rr.symbol,
                "timeframe": rr.timeframe,
                "params": rr.params,
                "personal_return_pct": rr.personal_return_pct,
                "baseline_return_pct": rr.baseline_return_pct,
                "fallback_reason": rr.fallback_reason or None,
                "persisted": rr.persisted,
            }
        )
        print(
            f"  {sym}: personal={rr.personal_return_pct:.3f}% "
            f"baseline={rr.baseline_return_pct:.3f}% "
            f"fallback={rr.fallback_reason or '-'} persist={rr.persisted}"
        )

    summary = compare_cohort(results)
    summary["days"] = args.days
    summary["timeframe"] = args.timeframe
    summary["synthetic"] = bool(args.synthetic)
    summary["generated_at"] = datetime.now(timezone.utc).isoformat()
    summary["symbols"] = [r.symbol for r in results]
    summary["details"] = details
    summary["primary_metric_definition"] = (
        "mean total_return_pct of bar-sim with chosen params "
        "(personal when kept, else tier defaults) vs always-tier baseline"
    )

    print(json.dumps({k: summary[k] for k in summary if k != "details"}, indent=2))
    print(
        f"PRIMARY {PRIMARY_METRIC}: personal={summary['personal_mean_total_return_pct']:.4f} "
        f"baseline={summary['baseline_mean_total_return_pct']:.4f} "
        f"delta={summary['delta']:.4f} equal_or_better={summary['equal_or_better']}"
    )

    out = args.out
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"wrote {out}")

    return 0 if summary["equal_or_better"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
