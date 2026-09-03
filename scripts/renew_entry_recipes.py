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


def _symbol_from_pos_key(key: str) -> str:
    """BEAT_USDT_4h → BEAT/USDT."""
    k = str(key or "")
    if "_" not in k:
        return k
    base = k.rsplit("_", 1)[0]  # drop tf
    if base.endswith("_USDT"):
        base = base[:-5] + "/USDT"
    elif base.endswith("USDT") and "/" not in base:
        base = base[:-4] + "/USDT"
    return base.replace("_", "/") if "/" not in base else base


def _ingest_orders_blob(data: dict | list, orders: list) -> None:
    if isinstance(data, list):
        orders.extend(data)
        return
    if not isinstance(data, dict):
        return
    if isinstance(data.get("orders"), list):
        orders.extend(data["orders"])
        return
    # nested: {"orders": {"orders": [...]}}
    inner = data.get("orders")
    if isinstance(inner, dict) and isinstance(inner.get("orders"), list):
        orders.extend(inner["orders"])


def _ingest_positions_blob(data: dict, positions: list) -> None:
    if not isinstance(data, dict):
        return
    posmap = data.get("positions")
    if isinstance(posmap, dict) and "positions" in posmap and isinstance(
        posmap.get("positions"), dict
    ):
        posmap = posmap["positions"]
    if not isinstance(posmap, dict):
        return
    for k, v in posmap.items():
        if not isinstance(v, dict):
            continue
        amt = float(v.get("amount") or 0)
        # include all keys for universe (open or historical cache)
        sym = v.get("symbol") or _symbol_from_pos_key(k)
        positions.append({"symbol": sym, "amount": amt, **v})


def _load_universe_from_local(args: argparse.Namespace) -> list[str]:
    from strategies.entry_recipe import build_symbol_universe, normalize_symbol

    if args.symbols:
        return [normalize_symbol(s) for s in args.symbols.split(",") if s.strip()]

    watchlist: list[Any] = []
    positions: list[Any] = []
    orders: list[Any] = []

    # Primary: data_manager-style watchlist.json
    for name in ("watchlist.json", "watchlist.dry_run_expansion.json"):
        p = ROOT / "data" / name
        if not p.exists():
            p = ROOT / name
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            coins = data.get("coins") if isinstance(data, dict) else data
            for c in coins or []:
                if isinstance(c, dict) and c.get("active") is False:
                    continue
                watchlist.append(c if isinstance(c, dict) else {"symbol": c})
        except Exception as e:
            print(f"watchlist.json warn {name}: {e}", file=sys.stderr)

    # data_manager.load_watchlist when importable
    try:
        from data_manager import load_watchlist

        for c in load_watchlist() or []:
            watchlist.append(c if isinstance(c, dict) else {"symbol": c})
    except Exception as e:
        print(f"load_watchlist warn: {e}", file=sys.stderr)

    # config strategies[] (explicit per-coin TF often 4h)
    try:
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        for c in cfg.get("watchlist") or cfg.get("coins") or []:
            watchlist.append(c if isinstance(c, dict) else {"symbol": c})
        for entry in cfg.get("strategies") or []:
            if entry.get("symbol"):
                watchlist.append(entry)
    except Exception as e:
        print(f"config watchlist warn: {e}", file=sys.stderr)

    # Local ledger files + demo_ledger backups (orders/positions)
    ledger_paths = [
        ROOT / "data" / "orders.demo.json",
        ROOT / "data" / "orders.json",
        ROOT / "data" / "orders.paper.json",
        ROOT / "data" / "positions.demo.json",
        ROOT / "data" / "positions.json",
        ROOT / "orders.demo.json",
        ROOT / "orders.json",
        ROOT / "orders.paper.json",
        ROOT / "positions.demo.json",
        ROOT / "positions.json",
    ]
    ledger_paths.extend(sorted((ROOT / "data").glob("demo_ledger*.json")))
    for p in ledger_paths:
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if "order" in p.name or (isinstance(data, dict) and "orders" in data):
            _ingest_orders_blob(data, orders)
        if "position" in p.name or (isinstance(data, dict) and "positions" in data):
            _ingest_positions_blob(data, positions)
        # full bundle
        if isinstance(data, dict) and "orders" in data and "positions" in data:
            _ingest_orders_blob(data, orders)
            _ingest_positions_blob(data, positions)

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
                _ingest_positions_blob(pos, positions)
        except Exception as e:
            print(f"mongo universe warn: {e}", file=sys.stderr)

    # liquid majors always (OHLCV reliability for 30d proof)
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
        # Prefer watchlist order first: rebuild priority list
        preferred = []
        seen = set()
        for c in watchlist:
            sym = normalize_symbol(
                c.get("symbol") if isinstance(c, dict) else str(c)
            )
            if sym in symbols and sym not in seen:
                preferred.append(sym)
                seen.add(sym)
        for s in symbols:
            if s not in seen:
                preferred.append(s)
                seen.add(s)
        symbols = preferred[: args.max_symbols]
    return symbols


def _default_timeframes(args: argparse.Namespace) -> list[str]:
    """Live coins often use 4h; volatile path uses 1h — renew both unless pinned."""
    raw = getattr(args, "timeframes", None) or args.timeframe or "4h,1h"
    # if user passed only --timeframe 1h historically, still allow multi via --timeframes
    tfs = [t.strip() for t in str(raw).split(",") if t.strip()]
    return tfs or ["4h", "1h"]


def _synthetic_ohlcv(
    days: int = 30, seed: int = 42, timeframe: str = "1h"
) -> "pd.DataFrame":
    import numpy as np
    import pandas as pd

    bars_per_day = {"15m": 96, "1h": 24, "4h": 6, "1d": 1}.get(timeframe, 24)
    freq = {"15m": "15min", "1h": "1h", "4h": "4h", "1d": "1D"}.get(timeframe, "1h")
    rng = np.random.default_rng(seed)
    n = max(days * bars_per_day, 80)
    # mean-reverting series so dip/RSI entries can fire
    rets = rng.normal(0.0, 0.008, size=n)
    price = 100 * np.exp(np.cumsum(rets))
    # inject a few dips
    for i in range(30, n, 40):
        price[i : i + 5] *= 0.96
    vol = rng.uniform(1e3, 5e3, size=n)
    vol[::17] *= 3.0
    ts = pd.date_range(end=datetime.now(timezone.utc), periods=n, freq=freq)
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
    parser.add_argument(
        "--timeframe",
        default="4h,1h",
        help="Comma-separated TFs to renew (default 4h,1h — live watchlist is often 4h)",
    )
    parser.add_argument("--timeframes", default="", help="Alias for --timeframe")
    parser.add_argument("--max-symbols", type=int, default=40)
    parser.add_argument("--symbols", default="")
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--mongo", action="store_true")
    parser.add_argument("--no-persist", action="store_true")
    parser.add_argument("--out", default="")
    parser.add_argument("--tier", default="volatile")
    args = parser.parse_args()
    if args.timeframes:
        args.timeframe = args.timeframes

    from strategies.entry_recipe import (
        PRIMARY_METRIC,
        add_indicators,
        compare_cohort,
        renew_symbol_params,
    )

    symbols = _load_universe_from_local(args)
    timeframes = _default_timeframes(args)
    print(
        f"universe n={len(symbols)} tfs={timeframes} "
        f"symbols={symbols[:15]}{'...' if len(symbols)>15 else ''}"
    )
    if "ZBT/USDT" in symbols:
        print("universe includes ZBT/USDT (watchlist)")

    results = []
    details = []
    for i, sym in enumerate(symbols):
        for tf in timeframes:
            if args.synthetic:
                df = _synthetic_ohlcv(args.days, seed=1000 + i * 17 + hash(tf) % 97, timeframe=tf)
            else:
                df = _fetch_ohlcv(sym, tf, args.days)
                if df is None:
                    df = _synthetic_ohlcv(
                        args.days, seed=1000 + i * 17 + hash(tf) % 97, timeframe=tf
                    )
            df = add_indicators(df)
            rr = renew_symbol_params(
                sym,
                df,
                timeframe=tf,
                tier=args.tier,
                persist=not args.no_persist,
            )
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
                f"  {sym} {tf}: personal={rr.personal_return_pct:.3f}% "
                f"baseline={rr.baseline_return_pct:.3f}% "
                f"fallback={rr.fallback_reason or '-'} persist={rr.persisted}"
            )

    summary = compare_cohort(results)
    summary["days"] = args.days
    summary["timeframes"] = timeframes
    summary["timeframe"] = ",".join(timeframes)
    summary["synthetic"] = bool(args.synthetic)
    summary["generated_at"] = datetime.now(timezone.utc).isoformat()
    summary["symbols"] = sorted({r.symbol for r in results})
    summary["n_symbol_tf_pairs"] = len(results)
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
