#!/usr/bin/env python3
"""Full memory enrichment from portfolio + watchlist + trades + news/social/macro + coin facts.

LEDGER SAFETY: read-only on orders/positions; writes only memory_*.

Pipeline (backward fill for books + watchlist):
  1) rebuild TradeMemory + CoinProfile from filled orders (Mongo)
  2) open positions → synthetic open trades + gap profiles
  3) optional historical coin-fact seeds
  4) portfolio snapshot events + significant fill events
  5–7) news / social / macro (optional --no-network)
  8) coin facts for positions ∪ active watchlist (CMC)
  9) RAG index (Mongo chunks ± Weaviate)

  python3 scripts/enrich_memory_full.py
  python3 scripts/enrich_memory_full.py --top 40 --no-network
  # Staging (Hermes network): railway ssh -s xagent-hermes -- python3 scripts/enrich_memory_full.py --top 40
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _load_seed_mod():
    import importlib.util

    p = _ROOT / "scripts" / "seed_memory_from_ledger.py"
    spec = importlib.util.spec_from_file_location("seed_memory_from_ledger", p)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


def profiles_from_open_positions(positions: list[dict], *, ledger_scope: str = "paper") -> int:
    """Ensure every open position has a CoinProfile (synthetic if no trade history)."""
    from intelligence.memory.models import CoinProfile, utc_now_iso
    from intelligence.memory.store import MemoryStore

    store = MemoryStore()
    existing = set()
    try:
        for p in store.list_profiles(limit=500) or []:
            existing.add(getattr(p, "symbol", None) or "")
    except Exception:
        from storage.mongo_client import get_database

        for d in get_database().memory_coin_profiles.find({}).limit(500):
            existing.add(d.get("symbol") or "")

    n = 0
    for row in positions:
        sym = row["symbol"]
        if sym in existing:
            continue
        dca = int(row.get("dca_rounds") or 0)
        notional = float(row.get("notional") or 0)
        size_bias = 1.0
        entry_bias = "neutral"
        risk = 0.5
        rationale = "open_position_only (no closed sells in memory yet)"
        if dca >= 3:
            size_bias = 0.85
            risk = 0.55
            rationale = f"open lot with dca_rounds={dca} — mild caution"
        elif dca >= 5:
            size_bias = 0.7
            entry_bias = "soft_block"
            risk = 0.65
            rationale = f"heavy DCA open lot rounds={dca}"
        prof = CoinProfile(
            symbol=sym,
            ledger_scope=ledger_scope,
            tenant_id="default",
            as_of=utc_now_iso(),
            trades_30d=0,
            buys_30d=1,
            sells_30d=0,
            dca_count_30d=dca,
            size_bias=size_bias,
            entry_bias=entry_bias,
            risk_score=risk,
            rationale=rationale,
            features={
                "source": "open_position_seed",
                "notional_usdt": round(notional, 2),
                "average_entry": row.get("average_entry"),
                "first_buy_at": row.get("first_buy_at"),
                "last_dca_at": row.get("last_dca_at"),
            },
        )
        if store.upsert_profile(prof):
            existing.add(sym)
            n += 1
    return n


def trade_memories_from_open_positions(positions: list[dict], *, ledger_scope: str = "paper") -> int:
    """Synthetic open TradeMemory rows so RAG/rebuild see the lot."""
    from intelligence.memory.models import TradeMemory, utc_now_iso
    from intelligence.memory.store import MemoryStore

    store = MemoryStore()
    n = 0
    for row in positions:
        sym = row["symbol"]
        entry = float(row.get("average_entry") or 0)
        notional = float(row.get("notional") or 0)
        amt = float(row.get("amount") or 0)
        tid = f"open|{ledger_scope}|{sym}|{str(row.get('first_buy_at') or '')[:19]}"
        tm = TradeMemory(
            trade_id=tid[:80],
            symbol=sym,
            entry_time=str(row.get("first_buy_at") or utc_now_iso()),
            exit_time="",
            direction="buy",
            entry_price=entry,
            exit_price=0.0,
            pnl_usdt=None,
            source="open_position",
            outcome="open",
            reason=f"open notional≈${notional:.0f} dca={row.get('dca_rounds')}",
            ledger_scope=ledger_scope,
            metadata={
                "amount": amt,
                "notional_usdt": notional,
                "dca_rounds": row.get("dca_rounds"),
                "seed": "open_position",
            },
        )
        if store.upsert_trade(tm):
            n += 1
    return n


def run_news(store) -> dict:
    from intelligence.memory.news_providers import poll_and_ingest_news
    from core.config import get_bot_config

    return poll_and_ingest_news(store, config=get_bot_config().raw)


def run_social(store) -> dict:
    from intelligence.memory.social_ingest import sync_social_memory

    return sync_social_memory(store) or {}


def run_macro(store) -> dict:
    from intelligence.macro.sync import sync_macro_context

    return sync_macro_context(store) or {}


def run_coin_facts(symbols: list[str], *, use_network: bool) -> dict:
    from intelligence.memory.coin_facts_ingest import sync_coin_facts
    from core.config import get_bot_config

    raw = get_bot_config().raw
    # force enabled for this enrichment run
    mem = dict(raw.get("memory") or {})
    cf = dict(mem.get("coin_facts") or {})
    cf["enabled"] = True
    cmc = dict((cf.get("sources") or {}).get("cmc_ai") or {})
    cmc["enabled"] = True
    cmc["max_coins_per_cycle"] = min(40, max(len(symbols), 1))
    cf["sources"] = {**(cf.get("sources") or {}), "cmc_ai": cmc}
    mem["coin_facts"] = cf
    raw = {**raw, "memory": mem}

    fetch_fn = None
    if not use_network:
        fix = _ROOT / "tests" / "fixtures" / "cmc_ai"

        def fetch_fn(url: str) -> str:  # type: ignore
            if "latest-updates" in url:
                return (fix / "allora_latest_updates.html").read_text()
            if "price-analysis" in url:
                return (fix / "allora_price_analysis.html").read_text()
            if "price-prediction" in url:
                return (fix / "allora_price_prediction.html").read_text()
            return ""

    return sync_coin_facts(
        config_raw=raw,
        symbols=symbols[: int(cmc.get("max_coins_per_cycle") or 40)],
        fetch_fn=fetch_fn,
    )


def run_rag_index(store) -> dict:
    try:
        from intelligence.memory.rag_index import index_store_into_rag

        return index_store_into_rag(store) or {}
    except Exception as e:
        return {"error": str(e)[:200]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--no-network", action="store_true", help="Skip RSS/social/live CMC")
    ap.add_argument(
        "--positions-file",
        type=Path,
        default=next(
            (
                p
                for p in (_ROOT / "data" / "positions.paper.json", _ROOT / "positions.paper.json")
                if p.is_file()
            ),
            _ROOT / "data" / "positions.paper.json",
        ),
    )
    ap.add_argument("--verify", default="ALLO/USDT,WLD/USDT,ZBT/USDT")
    args = ap.parse_args()

    seed = _load_seed_mod()
    from intelligence.memory.store import MemoryStore
    from storage.mongo_client import get_database

    store = MemoryStore()
    summary: dict = {}

    # 1) orders + trade_history rebuild
    try:
        summary["rebuild"] = seed.rebuild_memory()
        print("1 rebuild:", summary["rebuild"])
    except Exception as e:
        summary["rebuild_error"] = str(e)[:200]
        print("1 rebuild_error:", e)

    positions = seed._open_positions_from_file(args.positions_file)
    print(f"open positions: {len(positions)} from {args.positions_file.name}")

    # 2) synthetic open trades + profiles for gaps
    summary["open_trades"] = trade_memories_from_open_positions(positions)
    summary["open_profiles"] = profiles_from_open_positions(positions)
    print(f"2 open_trades={summary['open_trades']} open_profiles={summary['open_profiles']}")

    # 3) ALLO narrative seed
    try:
        summary["allo"] = seed.seed_allo_historical()
        print("3 allo:", {k: v for k, v in summary["allo"].items() if k != "ids"})
    except Exception as e:
        print("3 allo_error:", e)

    # 4) portfolio snapshots + large fills
    summary["open_facts"] = seed.seed_open_position_facts(positions, top=args.top)
    summary["order_facts"] = seed.seed_order_trade_events(max_events=100)
    print(f"4 open_facts={summary['open_facts']} order_facts={summary['order_facts']}")

    use_net = not args.no_network
    if use_net:
        try:
            summary["news"] = run_news(store)
            print("5 news:", summary["news"])
        except Exception as e:
            summary["news_error"] = str(e)[:200]
            print("5 news_error:", e)
        try:
            summary["social"] = run_social(store)
            print("6 social:", json.dumps(summary["social"], default=str)[:300])
        except Exception as e:
            summary["social_error"] = str(e)[:200]
            print("6 social_error:", e)
        try:
            summary["macro"] = run_macro(store)
            print("7 macro:", json.dumps(summary["macro"], default=str)[:300])
        except Exception as e:
            summary["macro_error"] = str(e)[:200]
            print("7 macro_error:", e)
    else:
        print("5-7 skipped (--no-network)")

    # 8) coin facts for open positions ∪ watchlist (network CMC if allowed)
    syms = [p["symbol"] for p in positions[: args.top]]
    try:
        from data_manager import load_effective_watchlist

        for coin in load_effective_watchlist() or []:
            if not (coin or {}).get("active", True):
                continue
            s = str((coin or {}).get("symbol") or "").strip().upper()
            if s and "/" not in s:
                s = f"{s}/USDT"
            if s and s not in syms and not s.startswith("TEST"):
                syms.append(s)
    except Exception as e:
        print("8 watchlist_merge_skip:", e)
    # Cap for API budget (positions already first)
    syms = syms[: max(args.top, 40)]
    print(f"8 universe n={len(syms)} (positions+watchlist, cap={max(args.top, 40)})")
    try:
        summary["coin_facts"] = run_coin_facts(syms, use_network=use_net)
        print("8 coin_facts:", summary["coin_facts"])
    except Exception as e:
        summary["coin_facts_error"] = str(e)[:200]
        print("8 coin_facts_error:", e)

    # 9) large moves → trigger attribution (uses CMC quotes when networked)
    if use_net:
        try:
            from intelligence.memory.move_attribution import sync_move_attribution

            summary["move_attribution"] = sync_move_attribution(
                store, symbols=syms[:40]
            )
            print("9 move_attribution:", summary["move_attribution"])
        except Exception as e:
            print("9 move_attribution_error:", e)
    else:
        print("9 move_attribution skipped (--no-network)")

    # 10) RAG index
    try:
        summary["rag"] = run_rag_index(store)
        print("10 rag:", json.dumps(summary["rag"], default=str)[:300])
    except Exception as e:
        print("10 rag_error:", e)

    db = get_database()
    summary["mongo"] = {
        "memory_market_events": db.memory_market_events.count_documents({}),
        "memory_trades": db.memory_trades.count_documents({}),
        "memory_coin_profiles": db.memory_coin_profiles.count_documents({}),
        "memory_lessons": db.memory_lessons.count_documents({}),
    }
    print("mongo:", summary["mongo"])

    # coverage open vs profiles
    open_set = {p["symbol"] for p in positions}
    prof = {d.get("symbol") for d in db.memory_coin_profiles.find({}, {"symbol": 1})}
    print(
        f"coverage profiles: {len(open_set & prof)}/{len(open_set)} open "
        f"missing={sorted(open_set - prof)[:12]}"
    )

    # verify
    import importlib.util

    p = _ROOT / "scripts" / "seed_coin_facts.py"
    spec = importlib.util.spec_from_file_location("seed_coin_facts", p)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    for sym in [s.strip() for s in args.verify.split(",") if s.strip()]:
        v = mod.verify_policy(sym)
        print(
            f"verify {sym}: facts={v.get('fact_event_count')} "
            f"mult={v.get('policy', {}).get('size_mult')} "
            f"codes={[c for c in (v.get('policy') or {}).get('reason_codes') or [] if str(c).startswith('fact_')]}"
        )

    print("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
