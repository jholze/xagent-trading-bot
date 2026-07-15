"""Regression: legacy demo read vs compound write split under multi-tenant."""

from __future__ import annotations

import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.models import TradeOrder, TradeResult
from core.tenant_context import DEFAULT_TENANT
from scripts.repair_tenant_ledgers import repair_tenant_ledgers
from storage.ledger_merge import merge_operator_ledger_scope, merge_order_lists
from storage.mongo_client import TEST_DB_NAME, drop_database
from storage.mongo_ledger import MongoLedgerStore
from storage.tenant_keys import compound_ledger_id


class TestLedgerSplitMerge(unittest.TestCase):
    def setUp(self):
        os.environ["PYTEST_RUNNING"] = "1"
        os.environ["MONGODB_DB"] = TEST_DB_NAME
        os.environ["MULTI_TENANT_ENABLED"] = "1"
        drop_database(test=True)
        self.store = MongoLedgerStore(test=True)

    def tearDown(self):
        drop_database(test=True)
        os.environ.pop("MULTI_TENANT_ENABLED", None)

    def _seed_split_demo_ledgers(self):
        coll = self.store._collection("orders")
        legacy_orders = [
            {"id": "a1", "display_seq": 1, "status": "filled", "side": "buy", "symbol": "OLD/USDT"},
            {"id": "a2", "display_seq": 2, "status": "filled", "side": "sell", "symbol": "OLD/USDT"},
        ]
        coll.replace_one(
            {"_id": "demo"},
            {"_id": "demo", "ledger_scope": "demo", "orders": legacy_orders},
            upsert=True,
        )
        coll.replace_one(
            {"_id": compound_ledger_id(DEFAULT_TENANT, "demo")},
            {
                "_id": compound_ledger_id(DEFAULT_TENANT, "demo"),
                "tenant_id": DEFAULT_TENANT,
                "ledger_scope": "demo",
                "orders": legacy_orders + [
                    {
                        "id": "a3",
                        "display_seq": 3,
                        "status": "filled",
                        "side": "buy",
                        "symbol": "NEW/USDT",
                    },
                ],
            },
            upsert=True,
        )

    def test_merge_order_lists_unions_compound_only_rows(self):
        legacy = [{"id": "1", "display_seq": 1, "symbol": "A/USDT"}]
        compound = [
            {"id": "1", "display_seq": 1, "symbol": "A/USDT"},
            {"id": "2", "display_seq": 2, "symbol": "B/USDT"},
        ]
        merged = merge_order_lists(legacy, compound)
        self.assertEqual([o["id"] for o in merged], ["1", "2"])

    def test_merge_then_read_write_same_compound_doc(self):
        self._seed_split_demo_ledgers()
        merge_operator_ledger_scope(scope="demo", dry_run=False, test=True, delete_legacy=True)

        loaded = self.store.load_orders("demo", tenant_id=DEFAULT_TENANT)
        self.assertEqual(len(loaded["orders"]), 3)
        self.assertEqual(loaded["orders"][-1]["symbol"], "NEW/USDT")
        self.assertIsNone(self.store._collection("orders").find_one({"_id": "demo"}))

        data = self.store.load_orders("demo", tenant_id=DEFAULT_TENANT)
        data["orders"].append(
            {
                "id": "a4",
                "display_seq": 4,
                "status": "filled",
                "side": "buy",
                "symbol": "SAVED/USDT",
                "ledger_scope": "demo",
            }
        )
        self.store.save_orders(data, "demo", tenant_id=DEFAULT_TENANT)
        reloaded = self.store.load_orders("demo", tenant_id=DEFAULT_TENANT)
        self.assertEqual(len(reloaded["orders"]), 4)
        self.assertEqual(reloaded["orders"][-1]["symbol"], "SAVED/USDT")

    def test_data_manager_load_orders_reads_merged_compound(self):
        self._seed_split_demo_ledgers()
        merge_operator_ledger_scope(scope="demo", dry_run=False, test=True, delete_legacy=True)

        cfg = {
            "trading_mode": "live",
            "architecture": {"ledger_backend": "mongo"},
            "multi_tenant": {"enabled": True},
        }
        loaded = self.store.load_orders("demo", tenant_id=DEFAULT_TENANT)
        symbols = [o.get("symbol") for o in loaded.get("orders", [])]
        self.assertIn("NEW/USDT", symbols)

    def test_repair_then_merge_keeps_operator_rows(self):
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
        repair_tenant_ledgers(scope="paper", target_tenant="henry", dry_run=False, test=True)
        merge_operator_ledger_scope(scope="paper", dry_run=False, test=True, delete_legacy=True)

        default = self.store.load_orders("paper", tenant_id=DEFAULT_TENANT)
        henry = self.store.load_orders("paper", tenant_id="henry")
        self.assertEqual([o["symbol"] for o in default["orders"]], ["OP/USDT"])
        self.assertEqual([o["symbol"] for o in henry["orders"]], ["HENRY/USDT"])
        self.assertIsNone(coll.find_one({"_id": "paper"}))


if __name__ == "__main__":
    unittest.main()