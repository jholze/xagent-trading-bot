#!/usr/bin/env python3
"""Export/import demo ledger documents (orders, positions, trade_history) as JSON."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SCOPE = "demo"


def _ensure_demo_mongo() -> None:
    os.environ.setdefault("DEMO_MODE", "1")
    os.environ.setdefault("DEMO_LEDGER_BACKEND", "mongo")


def export_bundle() -> dict:
    _ensure_demo_mongo()
    from storage.ledger_router import resolve_store

    store = resolve_store(SCOPE)
    return {
        "scope": SCOPE,
        "orders": store.load_orders(SCOPE),
        "positions": store.load_positions(SCOPE),
        "trade_history": store.load_trade_history(SCOPE),
    }


def import_bundle(payload: dict) -> dict:
    _ensure_demo_mongo()
    from storage.ledger_router import resolve_store
    from strategies.positions import bootstrap_positions, flush_positions

    store = resolve_store(SCOPE)
    orders = payload.get("orders") or {}
    positions = payload.get("positions") or {}
    history = payload.get("trade_history") or {}

    store.save_orders(orders, SCOPE)
    store.save_trade_history(history, SCOPE)
    store.save_positions(positions, SCOPE)

    bootstrap_positions(scope=SCOPE)
    flush_positions(scope=SCOPE, force=True)

    from data_manager import reconcile_demo_trade_history_on_startup
    from services.ledger_sync import sync_positions_on_startup

    sync_positions_on_startup()
    reconcile_demo_trade_history_on_startup()

    return {
        "orders": len((store.load_orders(SCOPE).get("orders") or [])),
        "positions": len((store.load_positions(SCOPE).get("positions") or {})),
        "virtual_balance": (store.load_trade_history(SCOPE).get("virtual_balance")),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Export/import demo Mongo ledger bundle")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("export", help="Write demo ledger JSON to stdout")

    imp = sub.add_parser("import", help="Load demo ledger JSON into current Mongo")
    imp.add_argument("--file", help="Bundle file (default: stdin)")

    args = parser.parse_args()

    if args.cmd == "export":
        json.dump(export_bundle(), sys.stdout)
        return 0

    if args.file:
        with open(args.file, encoding="utf-8") as f:
            payload = json.load(f)
    else:
        payload = json.load(sys.stdin)

    stats = import_bundle(payload)
    print(
        f"[import] demo ledger: orders={stats['orders']} "
        f"positions={stats['positions']} cash={stats['virtual_balance']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())