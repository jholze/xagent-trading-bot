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
MIN_ORDERS = 50  # legacy bundle only; fresh_start bundles may have 0 orders

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

    # test=True targets xagent_pytest — never use for Railway xagent_test ledger.
    store = MongoLedgerStore(test=False)
    existing = store.load_orders(SCOPE)
    order_count = len(existing.get("orders", []))
    if order_count >= MIN_ORDERS:
        print(f"[seed] demo orders={order_count} — no seed needed")
        return 0
    if order_count > 0:
        print(
            f"[seed] demo orders={order_count} — keeping existing ledger "
            f"(fresh_start only when empty)"
        )
        return 0

    orders = _load_seed("orders.json")
    history = _load_seed("history.json")
    fresh = bool(orders and orders.get("fresh_start")) or bool(history and history.get("fresh_start"))
    order_rows = len((orders or {}).get("orders", []))
    if not orders or (not fresh and order_rows < MIN_ORDERS):
        print("[seed] bundled orders.json missing or too small — keeping current ledger")
        return 0
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