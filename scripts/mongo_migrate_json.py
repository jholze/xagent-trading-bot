#!/usr/bin/env python3
"""Migrate JSON ledger files into MongoDB collections."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data_manager import (
    LIVE_TRADE_HISTORY_FILE,
    TRADE_HISTORY_FILE,
    resolve_orders_file,
    resolve_positions_file,
)
from storage.mongo_client import resolve_database_name
from storage.mongo_ledger import MongoLedgerStore

SCOPE_HISTORY_FILES = {
    "paper": TRADE_HISTORY_FILE,
    "live": LIVE_TRADE_HISTORY_FILE,
    "demo": f"{LIVE_TRADE_HISTORY_FILE.replace('.json', '.demo.json')}",
}


def _load_json(path: str) -> dict | None:
    if not Path(path).exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def migrate_scope(scope: str, *, dry_run: bool = False, test_db: bool = False) -> dict:
    orders_path = resolve_orders_file(scope)
    positions_path = resolve_positions_file(scope)
    history_path = SCOPE_HISTORY_FILES.get(scope)

    orders = _load_json(orders_path) or {"ledger_scope": scope, "orders": []}
    positions = _load_json(positions_path) or {"ledger_scope": scope, "positions": {}}
    history = _load_json(history_path) if history_path else None

    summary = {
        "scope": scope,
        "database": resolve_database_name(test=test_db),
        "orders": len(orders.get("orders", [])),
        "positions": len(positions.get("positions", {})),
        "trades": len((history or {}).get("trades", [])),
        "dry_run": dry_run,
    }

    if dry_run:
        print(
            f"[dry-run] scope={scope} db={summary['database']} "
            f"orders={summary['orders']} positions={summary['positions']} "
            f"trades={summary['trades']}"
        )
        return summary

    store = MongoLedgerStore(test=test_db)
    store.save_orders(orders, scope)
    store.save_positions(positions, scope)
    if history is not None:
        store.save_trade_history(history, scope)

    counts = store.count_documents()
    summary["mongo_counts"] = counts
    print(
        f"[applied] scope={scope} db={summary['database']} "
        f"imported orders={summary['orders']} positions={summary['positions']} "
        f"trades={summary['trades']} mongo={counts}"
    )

    mongo_orders = store.load_orders(scope)
    mongo_positions = store.load_positions(scope)
    mongo_history = store.load_trade_history(scope) if history is not None else None
    orders_match = mongo_orders.get("orders") == orders.get("orders")
    positions_match = mongo_positions.get("positions") == positions.get("positions")
    trades_match = (
        mongo_history.get("trades") == history.get("trades")
        if history is not None and mongo_history is not None
        else True
    )
    summary["roundtrip"] = {
        "orders": orders_match,
        "positions": positions_match,
        "trades": trades_match,
    }
    print(
        f"[roundtrip] scope={scope} orders={orders_match} "
        f"positions={positions_match} trades={trades_match}"
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate JSON ledgers to MongoDB")
    parser.add_argument("--scope", choices=["paper", "live", "demo"], required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test-db", action="store_true", help="Target xagent_test")
    args = parser.parse_args()

    if args.scope == "demo":
        os.environ.setdefault("DEMO_MODE", "1")

    try:
        migrate_scope(args.scope, dry_run=args.dry_run, test_db=args.test_db)
    except Exception as exc:
        print(f"migration failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())