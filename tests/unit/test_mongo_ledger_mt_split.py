"""Mongo ledger reads when legacy paper and default:paper diverge under multi-tenant."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.tenant_context import DEFAULT_TENANT
from storage.mongo_client import TEST_DB_NAME, drop_database
from storage.mongo_ledger import MongoLedgerStore
from storage.tenant_keys import compound_ledger_id


class TestMongoLedgerMultiTenantSplit(unittest.TestCase):
    def setUp(self):
        os.environ["PYTEST_RUNNING"] = "1"
        os.environ["MONGODB_DB"] = TEST_DB_NAME
        os.environ["MULTI_TENANT_ENABLED"] = "1"
        drop_database(test=True)
        self.store = MongoLedgerStore(test=True)
        coll = self.store._collection("orders")
        coll.replace_one(
            {"_id": "paper"},
            {
                "_id": "paper",
                "ledger_scope": "paper",
                "orders": [{"id": "op1", "symbol": "OP/USDT"}],
            },
            upsert=True,
        )
        coll.replace_one(
            {"_id": compound_ledger_id(DEFAULT_TENANT, "paper")},
            {
                "_id": compound_ledger_id(DEFAULT_TENANT, "paper"),
                "tenant_id": DEFAULT_TENANT,
                "ledger_scope": "paper",
                "orders": [{"id": "h1", "symbol": "HENRY/USDT"}],
            },
            upsert=True,
        )

    def tearDown(self):
        drop_database(test=True)
        os.environ.pop("MULTI_TENANT_ENABLED", None)

    def test_default_reads_operator_legacy_not_leaked_compound(self):
        loaded = self.store.load_orders("paper", tenant_id=DEFAULT_TENANT)
        symbols = [o["symbol"] for o in loaded["orders"]]
        self.assertEqual(symbols, ["OP/USDT"])

    def test_henry_reads_only_own_compound_doc(self):
        coll = self.store._collection("orders")
        coll.replace_one(
            {"_id": compound_ledger_id("henry", "paper")},
            {
                "_id": compound_ledger_id("henry", "paper"),
                "tenant_id": "henry",
                "ledger_scope": "paper",
                "orders": [{"id": "h1", "symbol": "HENRY/USDT"}],
            },
            upsert=True,
        )
        loaded = self.store.load_orders("paper", tenant_id="henry")
        symbols = [o["symbol"] for o in loaded["orders"]]
        self.assertEqual(symbols, ["HENRY/USDT"])

    def test_henry_never_reads_operator_legacy_or_default(self):
        loaded = self.store.load_orders("paper", tenant_id="henry")
        self.assertEqual(loaded["orders"], [])


if __name__ == "__main__":
    unittest.main()