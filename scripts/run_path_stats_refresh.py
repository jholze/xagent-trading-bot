#!/usr/bin/env python3
"""Multi-coin path-stats refresh (offline spike).

Universe (default):
  1) open positions
  2) symbols from recent filled trades (ledger orders)
  3) optional watchlist fill-up

Pure OHLCV episodes → summaries. Writes only when MEMORY_PATH_STATS=1
or --write with config enabled.

Rollback: MEMORY_PATH_STATS=0 / memory.path_stats.enabled=false — no writes;
this script never touches DE/trail.

Examples:
  python scripts/run_path_stats_refresh.py --dry-run --limit 60
  MEMORY_PATH_STATS=1 python scripts/run_path_stats_refresh.py --write
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _norm_symbol(raw: str) -> str:
    s = str(raw or "").strip().upper().replace("-", "/")
    if not s:
        return ""
    if "/" not in s and s.endswith("USDT") and len(s) > 4:
        s = f"{s[:-4]}/USDT"
    return s


def _symbols_from_positions(scope: str, limit: int) -> list[str]:
    from strategies.positions import is_open_position, load_positions, parse_position_key, positions

    load_positions(scope)
    out: list[str] = []
    for key, pos in list(positions.items()):
        if not is_open_position(pos):
            continue
        sym, _tf = parse_position_key(key)
        if not sym:
            sym = str(pos.get("symbol") or "").replace("_", "/")
        sym = _norm_symbol(sym)
        if sym and sym not in out:
            out.append(sym)
        if len(out) >= limit:
            break
    return out


def _order_ts(o: dict) -> str:
    for k in (
        "filled_at",
        "timestamp",
        "ts",
        "created_at",
        "updated_at",
        "time",
        "closed_at",
    ):
        v = o.get(k)
        if v:
            return str(v)
    return ""


def _symbols_from_trades(scope: str, *, max_orders: int, limit: int) -> list[str]:
    """Unique symbols from recent filled orders (newest first)."""
    from data_manager import load_orders

    doc = load_orders(scope) or {}
    orders = list(doc.get("orders") or [])
    # newest first
    orders.sort(key=_order_ts, reverse=True)
    out: list[str] = []
    scanned = 0
    for o in orders:
        if scanned >= max_orders:
            break
        status = str(o.get("status") or "").lower()
        if status and status not in ("filled", "closed", "complete", "completed"):
            # still count buy/sell rows without status as filled-ish if amount present
            if not (o.get("amount") or o.get("filled") or o.get("qty")):
                continue
        scanned += 1
        sym = _norm_symbol(str(o.get("symbol") or o.get("pair") or ""))
        if not sym:
            continue
        if sym not in out:
            out.append(sym)
        if len(out) >= limit:
            break
    return out


def _symbols_from_watchlist(limit: int) -> list[str]:
    for name in ("watchlist.demo.json", "watchlist.json"):
        path = ROOT / name
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        coins = data if isinstance(data, list) else data.get("coins") or data.get("symbols") or []
        out = []
        for c in coins:
            if isinstance(c, str):
                s = c if "/" in c else f"{c}/USDT"
            elif isinstance(c, dict):
                s = str(c.get("symbol") or "")
            else:
                continue
            s = _norm_symbol(s)
            if s and s not in out:
                out.append(s)
            if len(out) >= limit:
                break
        if out:
            return out
    return []


def _merge_universe(
    *,
    open_syms: list[str],
    trade_syms: list[str],
    watch_syms: list[str],
    limit: int,
) -> tuple[list[str], dict[str, str]]:
    """Priority: open → trades → watchlist. source map for report."""
    out: list[str] = []
    source: dict[str, str] = {}
    for s in open_syms:
        if s not in source:
            source[s] = "open"
            out.append(s)
        if len(out) >= limit:
            return out, source
    for s in trade_syms:
        if s not in source:
            source[s] = "trade"
            out.append(s)
        elif source[s] == "watch":
            source[s] = "trade"
        if len(out) >= limit:
            return out, source
    for s in watch_syms:
        if s not in source:
            source[s] = "watch"
            out.append(s)
        if len(out) >= limit:
            return out, source
    return out, source


def main() -> int:
    ap = argparse.ArgumentParser(description="Refresh multi-coin path-stats memory")
    ap.add_argument("--dry-run", action="store_true", help="Compute only; never write Mongo")
    ap.add_argument("--write", action="store_true", help="Write if path_stats enabled")
    ap.add_argument("--limit", type=int, default=80, help="Max unique symbols")
    ap.add_argument(
        "--trade-orders",
        type=int,
        default=500,
        help="How many recent filled orders to scan for symbols (default 500)",
    )
    ap.add_argument("--timeframe", default=None)
    ap.add_argument("--ohlcv-limit", type=int, default=None)
    ap.add_argument("--include-watchlist", action="store_true", default=True)
    ap.add_argument("--no-watchlist", action="store_true")
    ap.add_argument(
        "--no-trades",
        action="store_true",
        help="Skip symbols from recent ledger trades",
    )
    ap.add_argument("--out", default="", help="Write JSON report path")
    args = ap.parse_args()

    os.environ.setdefault("DEMO_MODE", "1")
    os.environ.setdefault("DEMO_LEDGER_BACKEND", "mongo")

    from core.config import get_bot_config
    from data_manager import resolve_ledger_scope
    from intelligence.memory.path_stats import (
        compute_path_stats_for_ohlcv,
        path_stats_enabled,
        upsert_path_summaries,
    )
    from services.market_service import MarketService

    cfg = get_bot_config().raw
    ps = ((cfg.get("memory") or {}).get("path_stats") or {})
    timeframe = args.timeframe or str(ps.get("timeframe") or "1h")
    ohlcv_limit = int(args.ohlcv_limit or ps.get("ohlcv_limit") or 500)
    bands_pct = ps.get("bands_pct") or [5, 8, 10, 12, 15, 20]
    bands = [float(x) / 100.0 for x in bands_pct]
    trough_lb = int(ps.get("trough_lookback") or 48)
    forward = int(ps.get("forward_bars") or 24)
    scope = resolve_ledger_scope() or "demo"

    enabled = path_stats_enabled(cfg)
    do_write = bool(args.write) and not args.dry_run and enabled
    if args.write and not enabled:
        print("WARN: --write ignored (path_stats disabled). Set MEMORY_PATH_STATS=1 or config enabled.")

    open_syms = _symbols_from_positions(scope, args.limit)
    trade_syms: list[str] = []
    if not args.no_trades:
        trade_syms = _symbols_from_trades(
            scope, max_orders=max(1, args.trade_orders), limit=args.limit
        )
    watch_syms: list[str] = []
    if not args.no_watchlist and args.include_watchlist:
        watch_syms = _symbols_from_watchlist(args.limit)

    symbols, sym_source = _merge_universe(
        open_syms=open_syms,
        trade_syms=trade_syms,
        watch_syms=watch_syms,
        limit=args.limit,
    )

    print(
        f"path_stats refresh symbols={len(symbols)} "
        f"(open={len(open_syms)} trades={len(trade_syms)} watch={len(watch_syms)}) "
        f"tf={timeframe} scope={scope} enabled={enabled} write={do_write}"
    )
    if not symbols:
        print("No symbols — empty open book / watchlist")
        return 1

    market = MarketService(config=cfg)
    report: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": scope,
        "timeframe": timeframe,
        "enabled": enabled,
        "write": do_write,
        "universe": {
            "open": open_syms,
            "from_trades": trade_syms,
            "watchlist": watch_syms,
            "merged": symbols,
            "source_by_symbol": sym_source,
        },
        "symbols": [],
        "summary": {
            "ok": 0,
            "thin": 0,
            "errors": 0,
            "bands": 0,
            "writes": 0,
            "n_open": len(open_syms),
            "n_trade_syms": len(trade_syms),
            "n_watch": len(watch_syms),
            "n_merged": len(symbols),
        },
    }

    all_summaries = []
    for sym in symbols:
        row: dict = {
            "symbol": sym,
            "source": sym_source.get(sym, "unknown"),
            "bands": [],
            "error": None,
        }
        try:
            df = market.fetch_ohlcv(sym, timeframe, limit=ohlcv_limit)
            if df is None or getattr(df, "empty", True):
                row["error"] = "no_ohlcv"
                report["summary"]["errors"] += 1
                report["symbols"].append(row)
                continue
            # DataFrame → [ts, o, h, l, c, v]
            rows = []
            cols = {str(c).lower(): c for c in getattr(df, "columns", [])}
            if "high" in cols and "low" in cols and "close" in cols:
                o_c = cols.get("open", cols["close"])
                h_c, l_c, c_c = cols["high"], cols["low"], cols["close"]
                v_c = cols.get("volume")
                for i, (_, r) in enumerate(df.iterrows()):
                    rows.append(
                        [
                            i,
                            float(r[o_c]),
                            float(r[h_c]),
                            float(r[l_c]),
                            float(r[c_c]),
                            float(r[v_c]) if v_c is not None else 0.0,
                        ]
                    )
            else:
                for i, (_, r) in enumerate(df.iterrows()):
                    vals = list(r.values)
                    if len(vals) < 4:
                        continue
                    rows.append(
                        [
                            i,
                            float(vals[0]),
                            float(vals[1]),
                            float(vals[2]),
                            float(vals[3]),
                            float(vals[4]) if len(vals) > 4 else 0.0,
                        ]
                    )

            summaries = compute_path_stats_for_ohlcv(
                sym,
                timeframe,
                rows,
                ledger_scope=scope,
                bands=bands,
                trough_lookback=trough_lb,
                forward_bars=forward,
            )
            all_summaries.extend(summaries)
            for s in summaries:
                row["bands"].append(
                    {
                        "band": s.band_key,
                        "n": s.n,
                        "quality": s.sample_quality,
                        "median_giveback": s.median_max_giveback,
                        "p_hit_trail_8": s.p_hit_trail,
                        "p_hit_ext": s.p_hit_extension,
                        "median_end_gain": s.median_end_gain,
                    }
                )
                report["summary"]["bands"] += 1
                if s.sample_quality == "ok":
                    report["summary"]["ok"] += 1
                else:
                    report["summary"]["thin"] += 1
        except Exception as e:
            row["error"] = str(e)[:160]
            report["summary"]["errors"] += 1
        report["symbols"].append(row)
        q = "ok" if any(b.get("quality") == "ok" for b in row["bands"]) else (
            "thin" if row["bands"] else "err"
        )
        print(f"  {sym}: {q} bands={len(row['bands'])} err={row['error']}")

    if do_write and all_summaries:
        # force=True only when user passed --write and we already checked enabled
        w = upsert_path_summaries(all_summaries, config=cfg, force=False)
        report["summary"]["writes"] = w
        print(f"wrote {w} docs to memory_path_stats")

    out = args.out
    if not out:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_dir = ROOT / "auswertungen"
        out_dir.mkdir(exist_ok=True)
        out = str(out_dir / f"path_stats_{ts}.json")
    Path(out).write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"report → {out}")
    print("summary", report["summary"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
