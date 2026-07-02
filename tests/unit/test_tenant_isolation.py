"""Cross-tenant ledger and positions isolation tests (Phase 0)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.models import TradeOrder
from core.tenant_context import DEFAULT_TENANT, tenant_context
from data_manager import load_orders, load_positions_document, save_orders
from services.order_service import OrderService
from storage.mongo_client import drop_database
from storage.mongo_ledger import MongoLedgerStore
from storage.tenant_keys import compound_ledger_id
from strategies.positions import clear_positions_memory, get_position, load_positions


class TestTenantIsolationMongo(unittest.TestCase):
    def setUp(self):
        os.environ["MONGODB_DB"] = "xagent_test"
        drop_database(test=True)
        self.store = MongoLedgerStore(test=True)

    def tearDown(self):
        drop_database(test=True)

    def test_compound_keys_isolate_orders(self):
        self.store.save_orders(
            {"orders": [{"symbol": "A/USDT"}], "migrated_from_trades": False},
            "paper",
            tenant_id="tenant_a",
        )
        self.store.save_orders(
            {"orders": [{"symbol": "B/USDT"}], "migrated_from_trades": False},
            "paper",
            tenant_id="tenant_b",
        )
        a = self.store.load_orders("paper", tenant_id="tenant_a")
        b = self.store.load_orders("paper", tenant_id="tenant_b")
        self.assertEqual(a["orders"][0]["symbol"], "A/USDT")
        self.assertEqual(b["orders"][0]["symbol"], "B/USDT")
        self.assertEqual(a["tenant_id"], "tenant_a")
        self.assertEqual(b["tenant_id"], "tenant_b")

    def test_legacy_default_fallback(self):
        coll = self.store._collection("orders")
        coll.replace_one(
            {"_id": "paper"},
            {
                "_id": "paper",
                "ledger_scope": "paper",
                "orders": [{"symbol": "LEGACY/USDT"}],
                "migrated_from_trades": False,
            },
            upsert=True,
        )
        loaded = self.store.load_orders("paper", tenant_id=DEFAULT_TENANT)
        self.assertEqual(loaded["orders"][0]["symbol"], "LEGACY/USDT")

    def test_save_writes_compound_id(self):
        self.store.save_orders(
            {"orders": [{"symbol": "X/USDT"}], "migrated_from_trades": False},
            "demo",
            tenant_id=DEFAULT_TENANT,
        )
        doc = self.store._collection("orders").find_one(
            {"_id": compound_ledger_id(DEFAULT_TENANT, "demo")}
        )
        self.assertIsNotNone(doc)
        self.assertEqual(doc["tenant_id"], DEFAULT_TENANT)
        self.assertEqual(doc["ledger_scope"], "demo")


class TestMigrateSingleToTenant(unittest.TestCase):
    def setUp(self):
        os.environ["MONGODB_DB"] = "xagent_test"
        drop_database(test=True)
        self.store = MongoLedgerStore(test=True)

    def tearDown(self):
        drop_database(test=True)

    def test_migration_idempotent(self):
        from scripts.migrate_single_to_tenant import migrate

        coll = self.store._collection("orders")
        coll.replace_one(
            {"_id": "paper"},
            {
                "_id": "paper",
                "ledger_scope": "paper",
                "orders": [{"symbol": "MIG/USDT"}],
                "migrated_from_trades": False,
            },
            upsert=True,
        )
        paper_key = f"orders:{compound_ledger_id(DEFAULT_TENANT, 'paper')}"
        first = migrate(test=True, dry_run=False)
        self.assertIn(paper_key, first["scopes"])
        loaded = self.store.load_orders("paper", tenant_id=DEFAULT_TENANT)
        self.assertEqual(loaded["orders"][0]["symbol"], "MIG/USDT")
        doc = coll.find_one({"_id": compound_ledger_id(DEFAULT_TENANT, "paper")})
        self.assertEqual(doc["tenant_id"], DEFAULT_TENANT)
        second = migrate(test=True, dry_run=False)
        self.assertNotIn(paper_key, second["scopes"])
        loaded_again = self.store.load_orders("paper", tenant_id=DEFAULT_TENANT)
        self.assertEqual(loaded_again["orders"][0]["symbol"], "MIG/USDT")


class TestTenantIsolationMongoContext(unittest.TestCase):
    def setUp(self):
        os.environ["MONGODB_DB"] = "xagent_test"
        drop_database(test=True)
        self.cfg = {
            "trading_mode": "paper",
            "architecture": {"ledger_backend": "mongo"},
        }

    def tearDown(self):
        drop_database(test=True)

    def test_context_scoped_mongo_no_leakage(self):
        marker_a = {"orders": [{"symbol": "TA/USDT"}], "migrated_from_trades": False}
        marker_b = {"orders": [{"symbol": "TB/USDT"}], "migrated_from_trades": False}

        with patch("data_manager.get_config", return_value=self.cfg):
            with tenant_context("tenant_a", scope="paper"):
                save_orders(marker_a, "paper")
            with tenant_context("tenant_b", scope="paper"):
                b = load_orders("paper")
                self.assertEqual(b["orders"], [])
                save_orders(marker_b, "paper")
            with tenant_context("tenant_a", scope="paper"):
                a = load_orders("paper")
                self.assertEqual(a["orders"][0]["symbol"], "TA/USDT")
            with tenant_context("tenant_b", scope="paper"):
                b = load_orders("paper")
                self.assertEqual(b["orders"][0]["symbol"], "TB/USDT")

    def test_positions_memory_isolated_per_tenant(self):
        with patch("data_manager.get_config", return_value=self.cfg), \
             patch("services.ledger_sync._build_positions_snapshot_from_orders", return_value={}), \
             patch(
                 "strategies.positions.load_positions_document",
                 side_effect=lambda scope, **kw: {"positions": {}, "ledger_scope": scope},
             ):
            clear_positions_memory(tenant_id="tenant_a", scope="paper")
            with tenant_context("tenant_a", scope="paper"):
                get_position("ISO/USDT", "4h")["amount"] = 5
            clear_positions_memory(tenant_id="tenant_b", scope="paper")
            with tenant_context("tenant_b", scope="paper"):
                pos = get_position("ISO/USDT", "4h")
                self.assertEqual(float(pos["amount"]), 0)


if __name__ == "__main__":
    unittest.main()