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
from data_manager import (
    load_orders,
    load_positions_document,
    save_orders,
    save_positions_document,
)
from services.order_service import OrderService
from storage.mongo_client import TEST_DB_NAME, drop_database
from storage.mongo_ledger import MongoLedgerStore
from storage.tenant_keys import compound_ledger_id
from strategies.positions import (
    clear_positions_memory,
    flush_positions,
    get_position,
    load_positions,
    update_position,
)


class TestTenantIsolationMongo(unittest.TestCase):
    def setUp(self):
        os.environ["PYTEST_RUNNING"] = "1"
        os.environ["MONGODB_DB"] = TEST_DB_NAME
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

    def test_trade_history_includes_tenant_fields(self):
        self.store.save_trade_history(
            {"trades": [{"symbol": "TH/USDT"}]},
            "paper",
            tenant_id="tenant_z",
        )
        loaded = self.store.load_trade_history("paper", tenant_id="tenant_z")
        self.assertEqual(loaded["tenant_id"], "tenant_z")
        self.assertEqual(loaded["ledger_scope"], "paper")


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
        self.assertIsNone(coll.find_one({"_id": "paper"}))
        second = migrate(test=True, dry_run=False)
        self.assertNotIn(paper_key, second["scopes"])
        loaded_again = self.store.load_orders("paper", tenant_id=DEFAULT_TENANT)
        self.assertEqual(loaded_again["orders"][0]["symbol"], "MIG/USDT")


class TestTenantIsolationMongoContext(unittest.TestCase):
    def setUp(self):
        os.environ["PYTEST_RUNNING"] = "1"
        os.environ["MONGODB_DB"] = TEST_DB_NAME
        drop_database(test=True)
        self.cfg = {
            "trading_mode": "paper",
            "architecture": {"ledger_backend": "mongo"},
        }

    def tearDown(self):
        drop_database(test=True)

    def test_context_scoped_mongo_no_leakage(self):
        marker_default = {
            "orders": [{"symbol": "DEF/USDT"}],
            "migrated_from_trades": False,
        }
        marker_a = {"orders": [{"symbol": "TA/USDT"}], "migrated_from_trades": False}
        marker_b = {"orders": [{"symbol": "TB/USDT"}], "migrated_from_trades": False}

        with patch("data_manager.get_config", return_value=self.cfg):
            save_orders(marker_default, "paper")
            default_no_ctx = load_orders("paper")
            self.assertEqual(default_no_ctx["orders"][0]["symbol"], "DEF/USDT")

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

            default_after = load_orders("paper")
            self.assertEqual(default_after["orders"][0]["symbol"], "DEF/USDT")

    def test_positions_mongo_isolated_per_tenant(self):
        with patch("data_manager.get_config", return_value=self.cfg):
            with tenant_context("tenant_a", scope="paper"):
                clear_positions_memory(scope="paper")
                update_position("ISO/USDT", "4h", "BUY", 1.0, amount_traded=100)
                flush_positions(scope="paper", force=True)
                doc_a = load_positions_document("paper")
                self.assertIn("ISO_USDT_4h", doc_a.get("positions", {}))

            with tenant_context("tenant_b", scope="paper"):
                clear_positions_memory(scope="paper")
                doc_b = load_positions_document("paper")
                self.assertEqual(doc_b.get("positions", {}), {})

            with tenant_context("tenant_a", scope="paper"):
                doc_a_reload = load_positions_document("paper")
                self.assertIn("ISO_USDT_4h", doc_a_reload.get("positions", {}))


if __name__ == "__main__":
    unittest.main()