#!/usr/bin/env python3
"""Smoke test: Mongo connectivity and basic write/read/delete on xagent_test."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage.mongo_client import TEST_DB_NAME, drop_database, ping_database
from storage.mongo_ledger import MongoLedgerStore


def main() -> int:
    print(f"Mongo smoke test — database: {TEST_DB_NAME}")
    try:
        ping_database(test=True)
        print("connect: OK")
    except Exception as exc:
        print(f"connect: FAILED ({exc})")
        return 1

    store = MongoLedgerStore(test=True)
    scope = "smoke"
    sample_orders = {
        "ledger_scope": scope,
        "orders": [{"id": "smoke-1", "symbol": "BTC/USDT", "status": "filled"}],
        "migrated_from_trades": False,
    }

    try:
        store.save_orders(sample_orders, scope)
        loaded = store.load_orders(scope)
        if loaded.get("orders") and loaded["orders"][0]["id"] == "smoke-1":
            print("write/read orders: OK")
        else:
            print(f"write/read orders: FAILED (got {loaded})")
            return 1

        drop_database(test=True)
        after = store.load_orders(scope)
        if not after.get("orders"):
            print("delete database: OK")
        else:
            print(f"delete database: FAILED (residual data: {after})")
            return 1
    except Exception as exc:
        print(f"ledger operations: FAILED ({exc})")
        return 1

    print("smoke test: PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())