#!/usr/bin/env python3
"""Seed / backfill coin-fact MarketEvents into memory_market_events (#103).

LEDGER SAFETY: only memory_* collections. Never orders/positions/trade_history.

Examples
  # Dry-run (print only)
  python3 scripts/seed_coin_facts.py --dry-run

  # Seed ALLO historical week (default file)
  python3 scripts/seed_coin_facts.py

  # Seed + verify DCA context flags for ALLO
  python3 scripts/seed_coin_facts.py --verify

  # Seed fixtures as if scraped today (for live cycle smoke)
  python3 scripts/seed_coin_facts.py --from-fixtures --symbols ALLO/USDT

  # List seeded events from Mongo
  python3 scripts/seed_coin_facts.py --list --symbol ALLO/USDT
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

DEFAULT_SEED = _ROOT / "data" / "coin_facts_seed" / "allo_week_2026-07.jsonl"
FIX_DIR = _ROOT / "tests" / "fixtures" / "cmc_ai"


def _load_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rows.append(json.loads(line))
    return rows


def _row_to_event(row: dict):
    from intelligence.memory.event_ingest import make_event_id
    from intelligence.memory.models import MarketEvent

    sym = str(row.get("symbol") or "").strip().upper()
    if sym and "/" not in sym:
        sym = f"{sym}/USDT"
    et = str(row.get("event_type") or "noise")
    src = str(row.get("source") or "cmc_ai_updates")
    ts = str(row.get("timestamp") or "")
    desc = str(row.get("description") or "")[:500]
    slug = str(row.get("slug") or "")
    day = (ts[:10] if ts else "undated")
    key = f"{slug}|{src}|{et}|{day}|{desc[:80]}|seed"
    eid = str(row.get("event_id") or make_event_id(src, key))
    meta = dict(row.get("metadata") or {})
    meta.setdefault("kind", "coin_fact")
    meta.setdefault("seed", True)
    if slug:
        meta.setdefault("slug", slug)
    return MarketEvent(
        event_id=eid,
        timestamp=ts,
        event_type=et,
        symbols=[sym] if sym else [],
        impact_score=float(row.get("impact_score") or 0),
        description=desc,
        source=src,
        url=str(row.get("url") or ""),
        metadata=meta,
    )


def seed_from_jsonl(path: Path, *, dry_run: bool = False) -> dict:
    from intelligence.memory.store import MemoryStore, memory_enabled

    if not memory_enabled():
        return {"ok": False, "error": "memory disabled"}
    rows = _load_jsonl(path)
    store = MemoryStore()
    written = 0
    ids = []
    for row in rows:
        ev = _row_to_event(row)
        if dry_run:
            print(
                f"DRY {ev.timestamp} {ev.event_type:28} {ev.impact_score:+.2f} "
                f"{ev.symbols} {ev.description[:60]}"
            )
            ids.append(ev.event_id)
            written += 1
            continue
        if store.upsert_event(ev):
            written += 1
            ids.append(ev.event_id)
    return {"ok": True, "path": str(path), "written": written, "ids": ids, "dry_run": dry_run}


def seed_from_fixtures(symbols: list[str], *, dry_run: bool = False) -> dict:
    """Parse local HTML fixtures and persist as today's facts (no network)."""
    from intelligence.memory.coin_facts_cmc import (
        parse_latest_updates_html,
        parse_price_analysis_html,
        parse_price_prediction_html,
        resolve_cmc_slug,
    )
    from intelligence.memory.coin_facts_ingest import persist_coin_fact
    from intelligence.memory.store import MemoryStore, memory_enabled

    if not memory_enabled():
        return {"ok": False, "error": "memory disabled"}
    store = MemoryStore()
    written = 0
    for sym in symbols:
        slug = resolve_cmc_slug(sym) or "allora"
        parsers = [
            ("latest_updates", FIX_DIR / "allora_latest_updates.html", parse_latest_updates_html),
            ("price_analysis", FIX_DIR / "allora_price_analysis.html", parse_price_analysis_html),
            ("price_prediction", FIX_DIR / "allora_price_prediction.html", parse_price_prediction_html),
        ]
        for ep, fpath, parse_fn in parsers:
            if not fpath.exists():
                continue
            drafts = parse_fn(fpath.read_text(encoding="utf-8"), symbol=sym, slug=slug)
            for d in drafts:
                if dry_run:
                    print(f"DRY fixture {sym} {d.event_type} {d.impact_score:+.2f} {d.description[:50]}")
                    written += 1
                    continue
                eid = persist_coin_fact(d, symbol=sym, slug=slug, store=store)
                if eid:
                    written += 1
    return {"ok": True, "written": written, "symbols": symbols, "dry_run": dry_run}


def list_facts(symbol: str | None, *, limit: int = 50) -> list:
    from intelligence.memory.store import MemoryStore

    store = MemoryStore()
    events = store.list_events(symbol=symbol, limit=limit) if symbol else store.list_events(limit=limit)
    # prefer coin-fact sources
    out = []
    for e in events:
        src = str(e.source or "")
        meta = e.metadata or {}
        if src.startswith("cmc_ai") or meta.get("kind") == "coin_fact" or meta.get("seed"):
            out.append(e)
    return out or events


def verify_policy(symbol: str) -> dict:
    """Force-load facts into context (even if config coin_facts.enabled=false)."""
    from intelligence.memory.coin_facts import apply_facts_to_context, summarize_facts_for_symbol
    from strategies.dca_policy import DcaContext, dca_policy_config, evaluate_dca_policy

    flags = summarize_facts_for_symbol(
        symbol,
        config_raw={
            "memory": {
                "coin_facts": {"enabled": True, "policy_apply": True, "lookback_hours": 24 * 400}
            }
        },
    )
    ctx = DcaContext(symbol=symbol, cash_mode="STEADY", fusion_size_mult=1.0, loss_pct=-8.0)
    apply_facts_to_context(
        ctx,
        config_raw={
            "memory": {
                "coin_facts": {"enabled": True, "policy_apply": True, "lookback_hours": 24 * 400}
            }
        },
        events=None,
    )
    # re-apply from flags if store empty path used defaults
    if flags.event_count and not ctx.fact_event_count:
        from intelligence.memory.coin_facts import apply_fact_flags_to_context

        apply_fact_flags_to_context(ctx, flags)

    # Ensure load via store with long lookback
    flags2 = summarize_facts_for_symbol(
        symbol,
        config_raw={
            "memory": {
                "coin_facts": {"enabled": True, "policy_apply": True, "lookback_hours": 24 * 400}
            }
        },
    )
    from intelligence.memory.coin_facts import apply_fact_flags_to_context

    apply_fact_flags_to_context(ctx, flags2)

    pcfg = dca_policy_config(
        {
            "policy": {
                "enabled": True,
                "shadow": False,
                "harvest_mode": "soft",
                "deploy_mult": 1.0,
                "size_mult_deploy": 99,
            }
        }
    )
    result = evaluate_dca_policy(ctx, pcfg)
    return {
        "symbol": symbol,
        "fact_event_count": ctx.fact_event_count,
        "fact_summary": ctx.fact_summary,
        "flags": {
            "hard_negative": ctx.fact_hard_negative,
            "unlock": ctx.fact_unlock,
            "profit_taking": ctx.fact_profit_taking,
            "flow_only": ctx.fact_flow_only,
            "utility": ctx.fact_utility,
            "volume_breakout": ctx.fact_volume_breakout,
            "min_impact": ctx.fact_min_impact,
        },
        "policy": {
            "skip": result.skip,
            "size_mult": result.size_mult,
            "reason_codes": list(result.reason_codes),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed coin facts into memory_market_events")
    ap.add_argument(
        "--file",
        type=Path,
        default=DEFAULT_SEED,
        help=f"JSONL seed file (default: {DEFAULT_SEED.name})",
    )
    ap.add_argument("--from-fixtures", action="store_true", help="Parse HTML fixtures instead of JSONL")
    ap.add_argument("--symbols", default="ALLO/USDT", help="Comma symbols for --from-fixtures")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list", action="store_true", help="List coin-fact events from Mongo")
    ap.add_argument("--symbol", default="ALLO/USDT", help="Symbol for --list / --verify")
    ap.add_argument("--verify", action="store_true", help="After seed, print DCA fact flags + policy")
    ap.add_argument("--limit", type=int, default=30)
    args = ap.parse_args()

    if args.list:
        events = list_facts(args.symbol if args.symbol else None, limit=args.limit)
        print(f"events={len(events)} symbol={args.symbol or '*'}")
        for e in events:
            print(
                f"  {e.timestamp} {e.event_type:28} {e.impact_score:+.2f} "
                f"src={e.source} {e.description[:70]}"
            )
        return 0

    if args.from_fixtures:
        syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
        out = seed_from_fixtures(syms, dry_run=args.dry_run)
    else:
        if not args.file.exists():
            print(f"missing seed file: {args.file}", file=sys.stderr)
            return 2
        out = seed_from_jsonl(args.file, dry_run=args.dry_run)

    print(json.dumps({k: v for k, v in out.items() if k != "ids"}, indent=2))
    if out.get("ids") and not args.dry_run:
        print(f"ids_sample={out['ids'][:3]}")

    if args.verify and not args.dry_run:
        report = verify_policy(args.symbol)
        print("--- verify policy ---")
        print(json.dumps(report, indent=2))
        if report.get("fact_event_count", 0) < 1:
            print("WARNING: no facts loaded — check lookback / Mongo / symbol", file=sys.stderr)
            return 1
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
