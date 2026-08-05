#!/usr/bin/env python3
"""Daily GIS monitor: Gate IST Leaders Top-N vs demo bot fills.

M0 foundation for Epic #203 / WS-5 #208.

  python3 scripts/gis_daily_monitor.py --day yesterday --top 20 --scope demo

Writes:
  auswertungen/gis/YYYY-MM-DD_monitor.json
  auswertungen/gis/YYYY-MM-DD_monitor.md

IST board uses live Gate 24h% tickers at run time (snapshot_at), joined to
orders_v2 fills for --day. Mongo is fail-open when unavailable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BOT_ROOT = Path(__file__).resolve().parents[1]
if str(BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(BOT_ROOT))

from services.gis_monitor.pure import (  # noqa: E402
    DEFAULT_ELIGIBLE_MIN_VOL,
    compute_kpis,
    is_gainer_source,
    join_leaders_to_fills,
    normalize_symbol,
    rank_leaders_from_tickers,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_day_arg(raw: str | None) -> str:
    """Return YYYY-MM-DD UTC day key."""
    today = _utc_now().date()
    s = (raw or "yesterday").strip().lower()
    if s in ("yesterday", "y"):
        return (today - timedelta(days=1)).isoformat()
    if s in ("today", "t"):
        return today.isoformat()
    # YYYY-MM-DD
    datetime.strptime(s, "%Y-%m-%d")
    return s


def fetch_gate_tickers() -> dict[str, Any]:
    import ccxt

    ex = ccxt.gate({"enableRateLimit": True, "options": {"defaultType": "spot"}})
    return ex.fetch_tickers() or {}


def load_recognized_from_gainer_state(
    path: Path | None = None,
) -> tuple[set[str] | None, str]:
    """Optional recognized set from local gainer_universe state (live_top)."""
    try:
        from services.gainer_universe.store import load_gainer_state, _state_path

        state_path = path or _state_path()
        if path is not None and not path.exists():
            return None, f"state_missing:{path}"
        state = load_gainer_state() if path is None else json.loads(path.read_text())
        if not isinstance(state, dict):
            return None, "state_invalid"
        syms: set[str] = set()
        for key in ("live_top", "eligible"):
            for row in state.get(key) or []:
                if isinstance(row, dict):
                    s = normalize_symbol(row.get("symbol") or "")
                    if s:
                        syms.add(s)
        if not syms:
            # Honest empty set → recall_proxy_reason=recognized_set_empty
            return set(), f"recognized_empty:{state_path}"
        return syms, f"gainer_state:{state_path}"
    except Exception as e:
        return None, f"gainer_state_error:{e}"


def load_fills_from_mongo(
    *,
    day_key: str,
    scope: str = "demo",
    tenant_id: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load filled orders for day. Fail-open."""
    meta: dict[str, Any] = {"mongo": "unavailable", "n": 0}
    uri = (os.environ.get("MONGO_URL") or os.environ.get("MONGODB_URI") or "").strip()
    dbn = (os.environ.get("MONGODB_DB") or os.environ.get("MONGODB_TEST_DB") or "").strip()
    if not uri:
        meta["mongo"] = "unavailable"
        meta["reason"] = "MONGO_URL unset"
        return [], meta
    if not dbn:
        dbn = "xagent_test"
    try:
        from pymongo import MongoClient

        cli = MongoClient(uri, serverSelectionTimeoutMS=8000)
        # force connect
        cli.admin.command("ping")
        col = cli[dbn]["orders_v2"]
        q: dict[str, Any] = {
            "status": "filled",
            "day_key": day_key,
            "ledger_scope": scope,
        }
        if tenant_id:
            q["tenant_id"] = tenant_id
        # also match without day_key via timestamps prefix (fallback)
        cursor = col.find(q)
        rows = list(cursor)
        if not rows:
            # fallback: timestamps.filled startswith day
            q2: dict[str, Any] = {"status": "filled", "ledger_scope": scope}
            if tenant_id:
                q2["tenant_id"] = tenant_id
            rows = []
            for doc in col.find(q2):
                ts = (doc.get("timestamps") or {}).get("filled") or doc.get("ts_event") or ""
                if str(ts).startswith(day_key):
                    rows.append(doc)
            meta["query"] = "timestamps_prefix_fallback"
        else:
            meta["query"] = "day_key"
        # slim for join
        fills = []
        for d in rows:
            fills.append(
                {
                    "symbol": d.get("symbol"),
                    "side": d.get("side"),
                    "status": d.get("status"),
                    "source": d.get("source"),
                    "exit_source": d.get("exit_source"),
                    "pnl": d.get("pnl"),
                    "tenant_id": d.get("tenant_id"),
                    "day_key": d.get("day_key") or day_key,
                    "execution": d.get("execution"),
                    "request": d.get("request"),
                }
            )
        meta["mongo"] = "ok"
        meta["db"] = dbn
        meta["n"] = len(fills)
        return fills, meta
    except Exception as e:
        meta["mongo"] = "unavailable"
        meta["reason"] = str(e)[:200]
        return [], meta


def load_fills_from_json(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("fills"), list):
        return data["fills"]
    raise ValueError(f"fills json must be list or {{fills: []}}: {path}")


def render_markdown(report: dict[str, Any]) -> str:
    day = report["day_key"]
    k = report["kpis"]
    leaders = report["leaders"]
    join_rows = report["join"]
    lines = [
        f"# GIS Monitor {day}",
        "",
        f"- generated_at: `{report.get('generated_at')}`",
        f"- snapshot_at: `{report.get('snapshot_at')}`",
        f"- ist_source: `{report.get('ist_source')}`",
        f"- scope: `{report.get('scope')}` mongo: `{report.get('mongo', {}).get('mongo')}`",
        f"- recognized_source: `{report.get('recognized_source')}`",
        "",
        "## KPIs",
        "",
        f"| KPI | Value |",
        f"|-----|-------|",
        f"| n_leaders | {k.get('n_leaders')} |",
        f"| eligible_coverage | {k.get('eligible_coverage')} |",
        f"| recall_proxy | {k.get('recall_proxy')} ({k.get('recall_proxy_reason')}) |",
        f"| missed_liquid_count (Top-{report.get('missed_rank_max', 10)}) | {k.get('missed_liquid_count')} |",
        f"| sleeve_hit_rate_eligible_top10 | {k.get('sleeve_hit_rate_eligible_top10')} |",
        f"| n_buy_fills / n_sell_fills | {k.get('n_buy_fills')} / {k.get('n_sell_fills')} |",
        f"| gainer_sell_expectancy | {k.get('gainer_sell_expectancy')} |",
        f"| gainer_sell_pnl_sum | {k.get('gainer_sell_pnl_sum')} |",
        "",
        "### pnl_by_source",
        "",
    ]
    pnl = k.get("pnl_by_source") or {}
    if not pnl:
        lines.append("_No sell PnL in window._")
    else:
        lines.append("| source | pnl |")
        lines.append("|--------|-----|")
        for src, v in pnl.items():
            lines.append(f"| {src} | {v:+.2f} |")
    lines += [
        "",
        "### buy_count_by_source",
        "",
        "```json",
        json.dumps(k.get("buy_count_by_source") or {}, indent=2),
        "```",
        "",
        f"### missed_liquid_leaders",
        "",
        ", ".join(k.get("missed_liquid_leaders") or []) or "_none_",
        "",
        "## IST Leaders",
        "",
        "| rank | symbol | pct_24h | vol | lev | eligible | recognized | gainer_buy | other_buy | missed | sell_pnl | note |",
        "|------|--------|---------|-----|-----|----------|------------|------------|-----------|--------|----------|------|",
    ]
    by_sym = {r["symbol"]: r for r in join_rows}
    for L in leaders:
        j = by_sym.get(L["symbol"]) or {}
        rec = j.get("recognized")
        rec_s = "—" if rec is None else ("Y" if rec else "N")
        lines.append(
            f"| {L.get('rank')} | {L.get('symbol')} | {L.get('pct_24h'):+.2f} | "
            f"{float(L.get('quote_vol') or 0):.0f} | "
            f"{'Y' if L.get('leverage') else 'N'} | "
            f"{'Y' if L.get('eligible') else 'N'} | {rec_s} | "
            f"{'Y' if j.get('bought_gainer') else 'N'} | "
            f"{'Y' if j.get('bought_other') else 'N'} | "
            f"{'Y' if j.get('missed') else 'N'} | "
            f"{float(j.get('sell_pnl') or 0):+.2f} | {j.get('note') or ''} |"
        )
    lines += [
        "",
        "## Rules",
        "",
        f"- eligible_min_quote_vol_usdt: **{DEFAULT_ELIGIBLE_MIN_VOL:.0f}**",
        "- min_price_filter: **false** (memes/alts allowed)",
        "- leverage: listed on board, **not** eligible",
        "",
        f"_Verdict: see 7d rollup later (M3). File: `auswertungen/gis/{day}_monitor.json`_",
        "",
    ]
    return "\n".join(lines)


def build_report(
    *,
    day_key: str,
    top_n: int,
    scope: str,
    tenant_id: str | None,
    tickers: dict[str, Any],
    fills: list[dict[str, Any]],
    mongo_meta: dict[str, Any],
    recognized: set[str] | None,
    recognized_source: str,
    missed_rank_max: int = 10,
) -> dict[str, Any]:
    snapshot_at = _utc_now().isoformat()
    leaders = rank_leaders_from_tickers(tickers, top_n=top_n)
    join_rows = join_leaders_to_fills(
        leaders,
        fills,
        recognized_symbols=recognized,
        missed_rank_max=missed_rank_max,
    )
    kpis = compute_kpis(
        leaders,
        join_rows,
        fills,
        recognized_symbols=recognized,
        top_k=top_n,
        missed_rank_max=missed_rank_max,
    )
    return {
        "day_key": day_key,
        "generated_at": snapshot_at,
        "snapshot_at": snapshot_at,
        "ist_source": "gate_live_tickers_24h_pct",
        "ist_note": (
            "M0 IST board is a live Gate REST 24h% snapshot at run time; "
            "day_key selects which demo fills to join (not historical day-return board)."
        ),
        "scope": scope,
        "tenant_id": tenant_id,
        "top_n": top_n,
        "missed_rank_max": missed_rank_max,
        "mongo": mongo_meta,
        "recognized_source": recognized_source,
        "n_recognized": len(recognized) if recognized is not None else None,
        "leaders": leaders,
        "join": join_rows,
        "kpis": kpis,
        "fills_summary": {
            "n": len(fills),
            "n_gainer_buys": sum(
                1
                for f in fills
                if str(f.get("side")).lower() == "buy" and is_gainer_source(f.get("source"))
            ),
        },
    }


def write_report(report: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    day = report["day_key"]
    jp = out_dir / f"{day}_monitor.json"
    mp = out_dir / f"{day}_monitor.md"
    jp.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    mp.write_text(render_markdown(report), encoding="utf-8")
    return jp, mp


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="GIS daily monitor: IST leaders vs demo fills")
    p.add_argument(
        "--day",
        default="yesterday",
        help="UTC day key: yesterday|today|YYYY-MM-DD (default yesterday)",
    )
    p.add_argument("--top", type=int, default=20, help="IST leaders depth (default 20)")
    p.add_argument("--scope", default="demo", help="ledger_scope (default demo)")
    p.add_argument("--tenant", default=None, help="optional tenant_id filter")
    p.add_argument(
        "--out-dir",
        default=str(BOT_ROOT / "auswertungen" / "gis"),
        help="output directory",
    )
    p.add_argument(
        "--fills-json",
        default=None,
        help="optional path to fills JSON list (skip mongo)",
    )
    p.add_argument(
        "--tickers-json",
        default=None,
        help="optional path to tickers dict JSON (skip Gate fetch)",
    )
    p.add_argument(
        "--no-recognized",
        action="store_true",
        help="do not load gainer_universe state for recall proxy",
    )
    p.add_argument(
        "--missed-rank-max",
        type=int,
        default=10,
        help="eligible rank<=N not bought counts as missed (default 10)",
    )
    args = p.parse_args(argv)

    day_key = parse_day_arg(args.day)
    print(f"GIS monitor day={day_key} top={args.top} scope={args.scope}", flush=True)

    if args.tickers_json:
        tickers = json.loads(Path(args.tickers_json).read_text(encoding="utf-8"))
        print(f"  tickers from file n={len(tickers)}", flush=True)
    else:
        print("  fetching Gate tickers…", flush=True)
        tickers = fetch_gate_tickers()
        print(f"  tickers n={len(tickers)}", flush=True)

    if args.fills_json:
        fills = load_fills_from_json(Path(args.fills_json))
        mongo_meta = {"mongo": "file", "path": args.fills_json, "n": len(fills)}
        print(f"  fills from file n={len(fills)}", flush=True)
    else:
        fills, mongo_meta = load_fills_from_mongo(
            day_key=day_key, scope=args.scope, tenant_id=args.tenant
        )
        print(f"  fills mongo={mongo_meta.get('mongo')} n={mongo_meta.get('n')}", flush=True)
        if mongo_meta.get("reason"):
            print(f"  mongo note: {mongo_meta.get('reason')}", flush=True)

    if args.no_recognized:
        recognized, rec_src = None, "disabled"
    else:
        recognized, rec_src = load_recognized_from_gainer_state()
        print(f"  recognized: {rec_src} n={len(recognized) if recognized else 0}", flush=True)

    report = build_report(
        day_key=day_key,
        top_n=max(1, int(args.top)),
        scope=args.scope,
        tenant_id=args.tenant,
        tickers=tickers,
        fills=fills,
        mongo_meta=mongo_meta,
        recognized=recognized,
        recognized_source=rec_src,
        missed_rank_max=int(args.missed_rank_max),
    )
    jp, mp = write_report(report, Path(args.out_dir))
    print(f"  wrote {jp}", flush=True)
    print(f"  wrote {mp}", flush=True)
    k = report["kpis"]
    print(
        f"  KPIs: leaders={k['n_leaders']} eligible={k['n_eligible_in_top']} "
        f"recall={k['recall_proxy']}({k['recall_proxy_reason']}) "
        f"missed_liquid={k['missed_liquid_count']} buys={k['n_buy_fills']} sells={k['n_sell_fills']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
