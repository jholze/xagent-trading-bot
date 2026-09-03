#!/usr/bin/env python3
"""Backfill Trading Memory for ~6 months across all ledger + watchlist coins.

This is the main "retroactive enrich" entrypoint:

  1) Rebuild TradeMemory + CoinProfile from filled orders (lookback 180d)
  2) Universe = all symbols seen in trades ∪ open positions ∪ watchlist
  3) Seed significant order events (large buys / painful sells)
  4) Coin facts for universe (CMC, batched)
  5) Social sync + delayed trade join
  6) Macro pressure events
  7) Reflect + DCA reflect
  8) RAG reindex

LEDGER SAFETY: read-only on orders/positions; writes only memory_*.

Usage:
  # Dry print plan only
  python3 scripts/backfill_memory_6m.py --dry-run

  # Full backfill (needs Mongo; prefer Hermes host on Railway)
  python3 scripts/backfill_memory_6m.py --days 180

  # Skip external HTTP (no CMC/RSS) — ledger + reflect only
  python3 scripts/backfill_memory_6m.py --days 180 --no-network

  railway ssh -s xagent-hermes -- python3 scripts/backfill_memory_6m.py --days 180
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _normalize_sym(raw: str) -> str:
    s = str(raw or "").strip().upper().replace("-", "/")
    if not s or s.startswith("TEST"):
        return ""
    if "/" not in s:
        s = f"{s}/USDT"
    return s


def collect_universe(
    *,
    lookback_days: int,
    store,
    scopes: tuple[str, ...] = ("demo", "live", "paper"),
) -> list[str]:
    """All symbols from memory trades + open positions + watchlist."""
    seen: set[str] = set()
    out: list[str] = []

    def add(raw: str) -> None:
        s = _normalize_sym(raw)
        if s and s not in seen:
            seen.add(s)
            out.append(s)

    # From rebuilt trades (after rebuild) or orders directly
    try:
        for t in store.list_trades(limit=5000) or []:
            add(getattr(t, "symbol", "") or "")
    except Exception as e:
        print(f"universe trades: {e}")

    try:
        from strategies.positions import list_active_positions

        for lot in list_active_positions() or []:
            add(str((lot or {}).get("symbol") or ""))
    except Exception as e:
        print(f"universe positions: {e}")

    # Local paper positions file fallback
    try:
        p = _ROOT / "data" / "positions.paper.json"
        if not p.is_file():
            p = _ROOT / "positions.paper.json"
        if p.is_file():
            import importlib.util

            seed_p = _ROOT / "scripts" / "seed_memory_from_ledger.py"
            spec = importlib.util.spec_from_file_location("seed_mem", seed_p)
            mod = importlib.util.module_from_spec(spec)
            assert spec.loader
            spec.loader.exec_module(mod)
            for row in mod._open_positions_from_file(p) or []:
                add(row.get("symbol") or "")
    except Exception as e:
        print(f"universe positions.file: {e}")

    try:
        from data_manager import load_effective_watchlist

        for coin in load_effective_watchlist() or []:
            if not (coin or {}).get("active", True):
                continue
            add(str((coin or {}).get("symbol") or ""))
    except Exception as e:
        print(f"universe watchlist: {e}")

    # Also scan raw orders for any symbol in lookback (in case trades empty)
    try:
        from storage.mongo_client import get_database

        db = get_database()
        since = datetime.now(timezone.utc) - timedelta(days=int(lookback_days))
        for scope in scopes:
            doc = db.orders.find_one({"_id": scope}) or db.orders.find_one(
                {"ledger_scope": scope}
            )
            if not doc:
                continue
            for o in doc.get("orders") or []:
                if str(o.get("status") or "").lower() != "filled":
                    continue
                ts = (o.get("timestamps") or {}).get("filled") or (
                    o.get("timestamps") or {}
                ).get("created")
                if ts:
                    try:
                        tss = str(ts).replace("Z", "+00:00")
                        dt = datetime.fromisoformat(tss[:32])
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        if dt < since:
                            continue
                    except Exception:
                        pass
                add(str(o.get("symbol") or ""))
    except Exception as e:
        print(f"universe orders: {e}")

    return out


def seed_order_events_6m(
    *,
    lookback_days: int = 180,
    max_events: int = 500,
    min_usdt: float = 200.0,
    min_loss: float = 30.0,
) -> int:
    """All significant fills in lookback → memory_market_events."""
    from intelligence.memory.event_ingest import make_event_id
    from intelligence.memory.models import MarketEvent
    from intelligence.memory.store import MemoryStore
    from storage.mongo_client import get_database

    db = get_database()
    store = MemoryStore()
    since = datetime.now(timezone.utc) - timedelta(days=int(lookback_days))
    candidates: list[tuple] = []
    for scope in ("demo", "live", "paper"):
        doc = db.orders.find_one({"_id": scope}) or db.orders.find_one(
            {"ledger_scope": scope}
        )
        if not doc:
            continue
        for o in doc.get("orders") or []:
            if str(o.get("status") or "").lower() != "filled":
                continue
            ts_raw = (o.get("timestamps") or {}).get("filled") or (
                o.get("timestamps") or {}
            ).get("created")
            if ts_raw:
                try:
                    tss = str(ts_raw).replace("Z", "+00:00")
                    dt = datetime.fromisoformat(tss[:32])
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if dt < since:
                        continue
                except Exception:
                    pass
            ex = o.get("execution") or {}
            req = o.get("request") or {}
            usdt = float(ex.get("usdt") or req.get("usdt") or 0)
            side = str(o.get("side") or "").lower()
            sym = _normalize_sym(str(o.get("symbol") or ""))
            if not sym:
                continue
            pnl = o.get("pnl")
            try:
                pnl_f = float(pnl) if pnl is not None else None
            except (TypeError, ValueError):
                pnl_f = None
            keep = usdt >= min_usdt or (
                side == "sell" and pnl_f is not None and abs(pnl_f) >= min_loss
            )
            if not keep:
                continue
            score = usdt + (abs(pnl_f) * 2 if pnl_f is not None else 0)
            candidates.append((score, o, usdt, side, sym, ts_raw, pnl_f, scope))

    candidates.sort(key=lambda x: -x[0])
    n = 0
    for score, o, usdt, side, sym, ts, pnl_f, scope in candidates[:max_events]:
        oid = str(o.get("id") or "")
        if side == "sell" and pnl_f is not None and pnl_f < -min_loss:
            et = "structure_risk" if pnl_f <= -200 else "profit_taking_narrative"
            impact = max(-0.8, min(-0.1, pnl_f / 1500.0))
            desc = (
                f"6m ledger sell {sym} scope={scope} pnl={pnl_f:.0f} "
                f"usdt={usdt:.0f} order={oid}"
            )
        elif side == "buy":
            et = "volume_breakout" if usdt >= 800 else "coin_fact"
            impact = 0.05
            desc = f"6m ledger buy {sym} scope={scope} usdt={usdt:.0f} order={oid}"
        else:
            continue
        eid = make_event_id("ledger_6m", f"{scope}|{oid}|{side}|{sym}")
        ev = MarketEvent(
            event_id=eid,
            timestamp=str(ts or "")[:32] or utc_now_fallback(),
            event_type=et,
            symbols=[sym],
            impact_score=impact,
            description=desc[:500],
            source="ledger_backfill_6m",
            metadata={
                "kind": "ledger_backfill",
                "lookback_days": lookback_days,
                "order_id": oid,
                "ledger_scope": scope,
                "side": side,
                "usdt": usdt,
                "pnl": pnl_f,
            },
        )
        if store.upsert_event(ev):
            n += 1
    return n


def utc_now_fallback() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_rebuild(lookback_days: int) -> dict:
    from intelligence.memory.rebuild import rebuild_from_orders
    from intelligence.memory.store import MemoryStore

    store = MemoryStore()
    # Rebuild primary active scopes (demo first on staging)
    results = {}
    for scope in (None, "demo", "live", "paper"):
        try:
            r = rebuild_from_orders(
                store,
                ledger_scope=scope,
                lookback_days=lookback_days,
            )
            key = r.get("ledger_scope") or str(scope or "auto")
            results[key] = r
        except Exception as e:
            results[str(scope)] = {"error": str(e)[:200]}
    return results


def run_coin_facts_batch(symbols: list[str], *, use_network: bool, batch: int = 40) -> dict:
    from core.config import get_bot_config
    from intelligence.memory.coin_facts_ingest import sync_coin_facts

    raw = dict(get_bot_config().raw or {})
    mem = dict(raw.get("memory") or {})
    cf = dict(mem.get("coin_facts") or {})
    cf["enabled"] = True
    cf["lookback_hours"] = max(int(cf.get("lookback_hours") or 72), 24 * 30)  # stretch if used
    sources = dict(cf.get("sources") or {})
    for sk in ("cmc_pro", "cmc_ai"):
        sc = dict(sources.get(sk) or {})
        sc["enabled"] = True
        sc["max_symbols_per_cycle"] = batch
        sc["max_coins_per_cycle"] = batch
        sources[sk] = sc
    cf["sources"] = sources
    mem["coin_facts"] = cf
    raw["memory"] = mem

    totals: dict[str, Any] = {"batches": 0, "symbols": len(symbols), "per_batch": []}
    if not use_network:
        totals["skipped"] = "no_network"
        return totals

    for i in range(0, len(symbols), batch):
        chunk = symbols[i : i + batch]
        try:
            stats = sync_coin_facts(config_raw=raw, symbols=chunk) or {}
            totals["per_batch"].append({"i": i, "n": len(chunk), **{k: stats.get(k) for k in list(stats)[:12]}})
            totals["batches"] += 1
            print(f"  coin_facts batch {i//batch+1}: n={len(chunk)} → {stats}")
        except Exception as e:
            totals["per_batch"].append({"i": i, "error": str(e)[:160]})
            print(f"  coin_facts batch error: {e}")
    return totals


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=180, help="Ledger lookback days (default 180 ≈ 6m)")
    ap.add_argument("--dry-run", action="store_true", help="Print universe/plan only")
    ap.add_argument("--no-network", action="store_true", help="Skip CMC/news HTTP")
    ap.add_argument("--skip-rebuild", action="store_true")
    ap.add_argument("--skip-facts", action="store_true")
    ap.add_argument("--skip-rag", action="store_true")
    ap.add_argument("--max-order-events", type=int, default=500)
    ap.add_argument("--fact-batch", type=int, default=40)
    args = ap.parse_args()

    days = max(30, int(args.days))
    print(f"=== Memory 6m backfill days={days} dry_run={args.dry_run} network={not args.no_network} ===")

    from intelligence.memory.store import MemoryStore, memory_enabled

    if not memory_enabled():
        print("FAIL: memory disabled")
        return 2

    store = MemoryStore()
    summary: dict[str, Any] = {"days": days, "started": utc_now_fallback()}

    # 1) rebuild
    if not args.skip_rebuild and not args.dry_run:
        print("1) rebuild_from_orders ...")
        summary["rebuild"] = run_rebuild(days)
        print("   ", json.dumps(summary["rebuild"], default=str)[:600])
    elif args.dry_run:
        print("1) rebuild SKIPPED (dry-run)")
    else:
        print("1) rebuild SKIPPED")

    # 2) universe
    print("2) collect universe ...")
    universe = collect_universe(lookback_days=days, store=store)
    summary["universe_n"] = len(universe)
    summary["universe_sample"] = universe[:30]
    print(f"   universe={len(universe)} sample={universe[:15]}")

    if args.dry_run:
        print("DRY-RUN done — no writes beyond optional prior rebuild")
        print(json.dumps(summary, indent=2, default=str)[:2000])
        return 0

    # 3) ledger order events
    print("3) seed order events (6m) ...")
    try:
        summary["order_events"] = seed_order_events_6m(
            lookback_days=days, max_events=int(args.max_order_events)
        )
        print(f"   order_events={summary['order_events']}")
    except Exception as e:
        summary["order_events_error"] = str(e)[:200]
        print("   error:", e)

    # 4) social + join
    print("4) social sync + join ...")
    try:
        from intelligence.memory.social_ingest import (
            join_social_events_to_trades,
            sync_social_memory,
        )

        if not args.no_network:
            summary["social"] = sync_social_memory(store) or {}
        else:
            summary["social"] = {"skipped": "no_network"}
        # force long delayed join
        summary["joined_trades"] = join_social_events_to_trades(
            store,
            config={
                "memory": {
                    "social": {
                        "join_window_hours": 12,
                        "join_window_hours_delayed": min(24 * 14, days * 24),
                    }
                }
            },
        )
        print(f"   social={summary.get('social')} joined={summary['joined_trades']}")
    except Exception as e:
        summary["social_error"] = str(e)[:200]
        print("   error:", e)

    # 5) macro
    print("5) macro sync ...")
    try:
        from intelligence.macro.sync import sync_macro_context

        summary["macro"] = sync_macro_context(store) or {}
        print(f"   macro={json.dumps(summary['macro'], default=str)[:300]}")
    except Exception as e:
        summary["macro_error"] = str(e)[:200]
        print("   error:", e)

    # 6) news + events (priority for backfill — tag universe coins)
    if not args.no_network:
        print("6) news + events (boosted, universe-tagged) ...")
        try:
            from core.config import get_bot_config
            from intelligence.memory.news_providers import poll_news_for_backfill
            from intelligence.macro.sync import sync_macro_context as _macro_again

            summary["news"] = poll_news_for_backfill(
                store,
                universe=universe,
                config=get_bot_config().raw,
                rounds=2,
            )
            # second macro pass after news so pressure events sit next to headlines
            try:
                summary["macro_after_news"] = _macro_again(store) or {}
            except Exception:
                pass
            print(f"   news={summary['news']}")
        except Exception as e:
            summary["news_error"] = str(e)[:200]
            print("   error:", e)
    else:
        print("6) news SKIPPED")

    # 7) coin facts for all universe symbols
    if not args.skip_facts:
        print(f"7) coin facts for {len(universe)} symbols ...")
        summary["coin_facts"] = run_coin_facts_batch(
            universe,
            use_network=not args.no_network,
            batch=int(args.fact_batch),
        )
    else:
        print("7) coin facts SKIPPED")

    # 8) reflect
    print("8) reflect ...")
    try:
        from intelligence.memory.dca_reflector import reflect_dca_policy
        from intelligence.memory.reflector import reflect
        from intelligence.memory.social_ingest import reflect_social

        summary["reflect"] = reflect(store)
        summary["reflect_social"] = reflect_social(store)
        summary["reflect_dca"] = reflect_dca_policy(store)
        print(
            f"   reflect={summary['reflect']} social={summary.get('reflect_social')} "
            f"dca={summary.get('reflect_dca')}"
        )
    except Exception as e:
        summary["reflect_error"] = str(e)[:200]
        print("   error:", e)

    # 9) RAG
    if not args.skip_rag:
        print("9) RAG index ...")
        try:
            from intelligence.memory.rag_index import index_store_into_rag

            summary["rag"] = index_store_into_rag(store) or {}
            print(f"   rag={json.dumps(summary['rag'], default=str)[:400]}")
        except Exception as e:
            summary["rag_error"] = str(e)[:200]
            print("   error:", e)
    else:
        print("9) RAG SKIPPED")

    # counts
    try:
        from storage.mongo_client import get_database

        db = get_database()
        summary["mongo"] = {
            "memory_market_events": db.memory_market_events.count_documents({}),
            "memory_trades": db.memory_trades.count_documents({}),
            "memory_coin_profiles": db.memory_coin_profiles.count_documents({}),
            "memory_lessons": db.memory_lessons.count_documents({}),
            "memory_rag_chunks": db.memory_rag_chunks.count_documents({})
            if "memory_rag_chunks" in db.list_collection_names()
            else None,
        }
        print("mongo:", summary["mongo"])
    except Exception as e:
        print("mongo count error:", e)

    summary["finished"] = utc_now_fallback()
    print("DONE")
    print(json.dumps({k: v for k, v in summary.items() if k != "universe_sample"}, default=str)[:2500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
