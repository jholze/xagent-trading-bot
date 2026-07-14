"""Repair script splits operator legacy book from leaked default compound rows."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.tenant_context import DEFAULT_TENANT
from scripts.repair_tenant_ledgers import repair_tenant_ledgers
from storage.mongo_client import TEST_DB_NAME, drop_database
from storage.mongo_ledger import MongoLedgerStore
from storage.tenant_keys import compound_ledger_id


class TestRepairTenantLedgers(unittest.TestCase):
    def setUp(self):
        os.environ["PYTEST_RUNNING"] = "1"
        os.environ["MONGODB_DB"] = TEST_DB_NAME
        drop_database(test=True)
        self.store = MongoLedgerStore(test=True)
        coll = self.store._collection("orders")
        coll.replace_one(
            {"_id": "paper"},
            {"_id": "paper", "ledger_scope": "paper", "orders": [{"id": "op1", "symbol": "OP/USDT"}]},
            upsert=True,
        )
        coll.replace_one(
            {"_id": compound_ledger_id(DEFAULT_TENANT, "paper")},
            {
                "_id": compound_ledger_id(DEFAULT_TENANT, "paper"),
                "tenant_id": DEFAULT_TENANT,
                "ledger_scope": "paper",
                "orders": [
                    {"id": "op1", "symbol": "OP/USDT"},
                    {"id": "h1", "symbol": "HENRY/USDT"},
                ],
            },
            upsert=True,
        )

    def tearDown(self):
        drop_database(test=True)

    def test_repair_moves_leaked_orders_to_target(self):
        repair_tenant_ledgers(scope="paper", target_tenant="henry", dry_run=False, test=True)
        default = self.store.load_orders("paper", tenant_id=DEFAULT_TENANT)
        henry = self.store.load_orders("paper", tenant_id="henry")
        self.assertEqual([o["symbol"] for o in default["orders"]], ["OP/USDT"])
        self.assertEqual([o["symbol"] for o in henry["orders"]], ["HENRY/USDT"])
        self.assertIsNone(self.store._collection("orders").find_one({"_id": "paper"}))


if __name__ == "__main__":
    unittest.main()