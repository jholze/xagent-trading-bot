#!/usr/bin/env python3
"""Run Phase 0 multi-tenant verification steps and print detailed evidence."""

from __future__ import annotations

import os
import subprocess
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.tenant_context import DEFAULT_TENANT, tenant_context
from data_manager import load_orders, save_orders
from storage.mongo_client import drop_database, get_database
from storage.mongo_ledger import MongoLedgerStore
from storage.tenant_keys import compound_ledger_id, is_legacy_doc
from scripts.migrate_single_to_tenant import migrate


def _isolation_exercise() -> None:
    os.environ["MONGODB_DB"] = "xagent_test"
    drop_database(test=True)
    cfg = {"trading_mode": "paper", "architecture": {"ledger_backend": "mongo"}}
    store = MongoLedgerStore(test=True)

    default_payload = {
        "orders": [{"symbol": "DEFAULT/USDT", "id": "d1"}],
        "migrated_from_trades": False,
    }
    store.save_orders(default_payload, "paper", tenant_id=DEFAULT_TENANT)
    print(f"WRITE default+paper orders={default_payload['orders'][0]['symbol']}")

    from unittest.mock import patch

    with patch("data_manager.get_config", return_value=cfg):
        with tenant_context("tenantA", scope="paper"):
            save_orders(
                {"orders": [{"symbol": "A/USDT", "id": "a1"}], "migrated_from_trades": False},
                "paper",
            )
            a_load = load_orders("paper")
            print(f"LOAD tenantA+paper len={len(a_load['orders'])} symbol={a_load['orders'][0]['symbol']}")

        with tenant_context("tenantB", scope="paper"):
            b_load = load_orders("paper")
            print(f"LOAD tenantB+paper len={len(b_load['orders'])} (expect 0)")
            assert b_load["orders"] == [], f"LEAK tenantB saw {b_load}"

        default_load = load_orders("paper")
        print(
            f"LOAD default+paper len={len(default_load['orders'])} "
            f"symbol={default_load['orders'][0]['symbol']}"
        )
        assert default_load["orders"][0]["symbol"] == "DEFAULT/USDT"

    coll = store._collection("orders")
    assert coll.find_one({"_id": compound_ledger_id("tenantA", "paper")})
    assert coll.find_one({"_id": compound_ledger_id("tenantB", "paper")}) is None
    print("ISOLATION_OK tenantA/tenantB/default")


def _migrate_apply_verify() -> None:
    os.environ["MONGODB_DB"] = "xagent_test"
    drop_database(test=True)
    db = get_database(test=True)
    coll = db["orders"]
    coll.replace_one(
        {"_id": "paper"},
        {
            "_id": "paper",
            "ledger_scope": "paper",
            "orders": [{"symbol": "PREMIG/USDT"}],
            "migrated_from_trades": False,
        },
        upsert=True,
    )
    store = MongoLedgerStore(test=True)
    pre = store.load_orders("paper", tenant_id=DEFAULT_TENANT)
    print(f"PRE_MIGRATE symbol={pre['orders'][0]['symbol']} legacy_exists={bool(coll.find_one({'_id': 'paper'}))}")

    stats = migrate(test=True, dry_run=False)
    print(f"APPLY migrated={stats['migrated']} legacy_deleted={stats['legacy_deleted']}")

    post = store.load_orders("paper", tenant_id=DEFAULT_TENANT)
    compound = coll.find_one({"_id": compound_ledger_id(DEFAULT_TENANT, "paper")})
    legacy = coll.find_one({"_id": "paper"})
    print(
        f"POST_MIGRATE symbol={post['orders'][0]['symbol']} "
        f"compound_tenant={compound.get('tenant_id')} legacy_gone={legacy is None}"
    )
    assert post["orders"][0]["symbol"] == "PREMIG/USDT"
    assert legacy is None
    assert not is_legacy_doc(compound)
    print("MIGRATE_APPLY_OK")


def main() -> int:
    step = sys.argv[1] if len(sys.argv) > 1 else "all"
    if step in ("isolation", "all"):
        _isolation_exercise()
    if step in ("migrate", "all"):
        _migrate_apply_verify()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())