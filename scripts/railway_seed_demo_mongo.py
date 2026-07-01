#!/usr/bin/env python3
"""Seed Railway demo Mongo ledger from bundled JSON when the DB looks empty."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEED_DIR = ROOT / "data" / "railway_seed"
SCOPE = "demo"
MIN_ORDERS = 50

sys.path.insert(0, str(ROOT))

os.environ.setdefault("DEMO_MODE", "1")
os.environ.setdefault("DEMO_LEDGER_BACKEND", "mongo")


def _load_seed(name: str) -> dict | None:
    path = SEED_DIR / name
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    from storage.mongo_client import ping_database, resolve_database_name
    from storage.mongo_ledger import MongoLedgerStore

    if not ping_database():
        print("[seed] Mongo ping failed — skipping")
        return 1

    store = MongoLedgerStore(test=resolve_database_name() == "xagent_test")
    existing = store.load_orders(SCOPE)
    order_count = len(existing.get("orders", []))
    if order_count >= MIN_ORDERS:
        print(f"[seed] demo orders={order_count} — no seed needed")
        return 0

    orders = _load_seed("orders.json")
    if not orders or len(orders.get("orders", [])) < MIN_ORDERS:
        print("[seed] bundled orders.json missing or too small")
        return 1

    history = _load_seed("trade_history.json")
    orders["ledger_scope"] = SCOPE
    store.save_orders(orders, SCOPE)
    if history:
        store.save_trade_history(history, SCOPE)

    from strategies.positions import bootstrap_positions, flush_positions

    bootstrap_positions(scope=SCOPE)
    flush_positions(scope=SCOPE, force=True)

    seeded = store.load_orders(SCOPE)
    positions = store.load_positions(SCOPE)
    print(
        f"[seed] applied demo ledger db={resolve_database_name()} "
        f"orders={len(seeded.get('orders', []))} "
        f"positions={len(positions.get('positions', {}))}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())