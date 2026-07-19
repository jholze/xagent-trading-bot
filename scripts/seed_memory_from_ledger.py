#!/usr/bin/env python3
"""Seed trading memory from local ledger + portfolio (no orders/positions writes).

1) rebuild TradeMemory + CoinProfile from Mongo orders (demo+live)
2) seed ALLO historical coin-facts JSONL
3) open positions (paper) → symbol-scoped portfolio fact events (top N by notional)
4) significant fills (large USDT / sells with pnl) → trade-linked market events

Usage:
  python3 scripts/seed_memory_from_ledger.py
  python3 scripts/seed_memory_from_ledger.py --top 20 --verify ALLO/USDT
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _open_positions_from_file(path: Path) -> list[dict]:
    if not path.exists():
        return []
    d = json.loads(path.read_text(encoding="utf-8"))
    pos = d.get("positions") or d
    out = []
    for k, v in (pos or {}).items():
        if not isinstance(v, dict):
            continue
        amt = float(v.get("amount") or 0)
        if amt <= 0:
            continue
        base = k.rsplit("_", 1)[0].replace("_", "/")
        if base.upper().startswith("TEST"):
            continue
        entry = float(v.get("average_entry") or v.get("entry_price") or 0)
        notional = amt * entry if entry > 0 else 0.0
        out.append(
            {
                "symbol": base if "/" in base else f"{base}/USDT",
                "amount": amt,
                "average_entry": entry,
                "notional": notional,
                "key": k,
                "dca_rounds": int(v.get("dca_rounds") or 0),
                "first_buy_at": v.get("first_buy_at"),
                "last_dca_at": v.get("last_dca_at"),
            }
        )
    out.sort(key=lambda r: -float(r["notional"] or 0))
    return out


def rebuild_memory() -> dict:
    from intelligence.memory.rebuild import rebuild_from_orders
    from intelligence.memory.store import MemoryStore

    store = MemoryStore()
    return rebuild_from_orders(store)


def seed_allo_historical() -> dict:
    from scripts.seed_coin_facts import DEFAULT_SEED, seed_from_jsonl

    # load via path (scripts not always a package)
    import importlib.util

    p = _ROOT / "scripts" / "seed_coin_facts.py"
    spec = importlib.util.spec_from_file_location("seed_coin_facts", p)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod.seed_from_jsonl(mod.DEFAULT_SEED, dry_run=False)


def seed_open_position_facts(positions: list[dict], *, top: int = 25) -> int:
    from intelligence.memory.event_ingest import make_event_id
    from intelligence.memory.models import MarketEvent, utc_now_iso
    from intelligence.memory.store import MemoryStore

    store = MemoryStore()
    n = 0
    as_of = utc_now_iso()
    day = as_of[:10]
    for row in positions[:top]:
        sym = row["symbol"]
        notional = float(row.get("notional") or 0)
        dca = int(row.get("dca_rounds") or 0)
        desc = (
            f"Open position {sym}: notional≈${notional:.0f} "
            f"entry={row.get('average_entry')} dca_rounds={dca}"
        )
        # Mild context only — not a hard policy signal
        impact = 0.0
        if dca >= 2:
            impact = -0.1  # already averaged down — slight caution
        eid = make_event_id(
            "portfolio_snapshot",
            f"{sym}|open|{day}|{notional:.0f}|{dca}",
        )
        ev = MarketEvent(
            event_id=eid,
            timestamp=as_of,
            event_type="structure_bias" if dca < 2 else "structure_risk",
            symbols=[sym],
            impact_score=impact,
            description=desc[:500],
            source="portfolio_snapshot",
            metadata={
                "kind": "coin_fact",
                "seed": "open_positions",
                "notional_usdt": notional,
                "dca_rounds": dca,
                "first_buy_at": row.get("first_buy_at"),
            },
        )
        if store.upsert_event(ev):
            n += 1
    return n


def seed_order_trade_events(*, max_events: int = 80) -> int:
    """Large fills and sells → symbol-scoped memory events."""
    from intelligence.memory.event_ingest import make_event_id
    from intelligence.memory.models import MarketEvent
    from intelligence.memory.store import MemoryStore
    from storage.mongo_client import get_database

    db = get_database()
    store = MemoryStore()
    n = 0
    candidates = []
    for scope in ("demo", "live", "paper"):
        doc = db.orders.find_one({"_id": scope}) or db.orders.find_one({"ledger_scope": scope})
        if not doc:
            continue
        for o in doc.get("orders") or []:
            if str(o.get("status") or "").lower() != "filled":
                continue
            ex = o.get("execution") or {}
            req = o.get("request") or {}
            usdt = float(ex.get("usdt") or req.get("usdt") or 0)
            side = str(o.get("side") or "").lower()
            sym = str(o.get("symbol") or "")
            if not sym:
                continue
            ts = (o.get("timestamps") or {}).get("filled") or (o.get("timestamps") or {}).get(
                "created"
            )
            pnl = o.get("pnl")
            try:
                pnl_f = float(pnl) if pnl is not None else None
            except (TypeError, ValueError):
                pnl_f = None
            score = usdt
            if side == "sell" and pnl_f is not None:
                score = max(score, abs(pnl_f) * 2)
            if usdt < 500 and (pnl_f is None or abs(pnl_f) < 50):
                continue
            candidates.append((score, o, usdt, side, sym, ts, pnl_f, scope))

    candidates.sort(key=lambda x: -x[0])
    for score, o, usdt, side, sym, ts, pnl_f, scope in candidates[:max_events]:
        oid = str(o.get("id") or "")
        if side == "sell" and pnl_f is not None and pnl_f < -50:
            et = "profit_taking_narrative" if pnl_f > -200 else "structure_risk"
            impact = max(-0.7, min(-0.15, pnl_f / 1000.0))
            desc = f"Past sell {sym} scope={scope} pnl={pnl_f:.0f} usdt={usdt:.0f} order={oid}"
        elif side == "buy" and usdt >= 1000:
            et = "volume_breakout"
            impact = 0.05
            desc = f"Past large buy {sym} scope={scope} usdt={usdt:.0f} order={oid}"
        else:
            continue
        eid = make_event_id("ledger_seed", f"{scope}|{oid}|{side}|{sym}")
        ev = MarketEvent(
            event_id=eid,
            timestamp=str(ts or "")[:32] or "2026-06-25T00:00:00Z",
            event_type=et,
            symbols=[sym if "/" in sym else f"{sym}/USDT"],
            impact_score=impact,
            description=desc[:500],
            source="ledger_seed",
            metadata={
                "kind": "coin_fact",
                "seed": "orders",
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=25, help="Top open positions by notional")
    ap.add_argument(
        "--positions-file",
        type=Path,
        default=_ROOT / "positions.paper.json",
    )
    ap.add_argument("--verify", default="ALLO/USDT")
    ap.add_argument("--skip-rebuild", action="store_true")
    args = ap.parse_args()

    summary: dict = {}

    if not args.skip_rebuild:
        try:
            summary["rebuild"] = rebuild_memory()
            print("rebuild:", json.dumps(summary["rebuild"], default=str)[:500])
        except Exception as e:
            summary["rebuild_error"] = str(e)[:200]
            print("rebuild_error:", e)

    try:
        summary["allo_seed"] = seed_allo_historical()
        print("allo_seed:", {k: v for k, v in summary["allo_seed"].items() if k != "ids"})
    except Exception as e:
        summary["allo_error"] = str(e)[:200]
        print("allo_error:", e)

    positions = _open_positions_from_file(args.positions_file)
    print(f"open_positions file={args.positions_file.name} n={len(positions)}")
    for r in positions[:10]:
        print(f"  {r['symbol']:14} ${r['notional']:.0f} dca={r['dca_rounds']}")

    summary["open_facts"] = seed_open_position_facts(positions, top=args.top)
    print("open_facts_written:", summary["open_facts"])

    summary["order_facts"] = seed_order_trade_events(max_events=80)
    print("order_facts_written:", summary["order_facts"])

    # counts
    from intelligence.memory.store import MemoryStore
    from storage.mongo_client import get_database

    db = get_database()
    summary["mongo"] = {
        "memory_market_events": db.memory_market_events.count_documents({}),
        "memory_trades": db.memory_trades.count_documents({}),
        "memory_coin_profiles": db.memory_coin_profiles.count_documents({}),
    }
    print("mongo:", summary["mongo"])

    if args.verify:
        import importlib.util

        p = _ROOT / "scripts" / "seed_coin_facts.py"
        spec = importlib.util.spec_from_file_location("seed_coin_facts", p)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader
        spec.loader.exec_module(mod)
        v = mod.verify_policy(args.verify)
        summary["verify"] = v
        print("verify:", json.dumps(v, indent=2))

    print("DONE", json.dumps({k: summary[k] for k in summary if k != "verify"}, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
