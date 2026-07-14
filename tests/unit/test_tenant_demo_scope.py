"""Regression: demo-mode staging must route tenants to demo ledger scope."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.tenant_context import DEFAULT_TENANT, tenant_context
from core.tenant_routing import resolve_incoming_tenant, tenant_cycle_context
from storage.mongo_client import TEST_DB_NAME, drop_database
from storage.mongo_ledger import MongoLedgerStore
from storage.tenant_keys import compound_ledger_id
from strategies.positions import load_positions, list_active_positions


class TestTenantDemoScopeRouting(unittest.TestCase):
    def setUp(self):
        os.environ["TELEGRAM_CHAT_ID"] = "111"
        os.environ["MULTI_TENANT_ENABLED"] = "1"

    def tearDown(self):
        os.environ.pop("TELEGRAM_CHAT_ID", None)
        os.environ.pop("MULTI_TENANT_ENABLED", None)

    @patch("data_manager.is_demo_mode", return_value=True)
    @patch("data_manager.resolve_ledger_scope", return_value="demo")
    @patch("core.tenant_routing.multi_tenant_enabled", return_value=True)
    @patch("storage.tenant_registry.find_tenant_by_owner_chat_id")
    def test_operator_telegram_route_uses_demo(self, mock_find, _mt, _scope, _demo):
        mock_find.return_value = None
        route = resolve_incoming_tenant(chat_id="111")
        self.assertEqual(route.tenant_id, DEFAULT_TENANT)
        self.assertEqual(route.scope, "demo")

    @patch("data_manager.is_demo_mode", return_value=True)
    @patch("data_manager.resolve_ledger_scope", return_value="demo")
    @patch("core.tenant_routing.multi_tenant_enabled", return_value=True)
    @patch("storage.tenant_registry.get_tenant")
    def test_default_price_cycle_uses_demo(self, mock_get, _mt, _scope, _demo):
        mock_get.return_value = {
            "tenant_id": DEFAULT_TENANT,
            "defaults": {"ledger_scope": "paper"},
            "telegram": {"owner_chat_id": "111"},
        }
        from core.tenant_context import resolve_tenant_scope

        with tenant_cycle_context(DEFAULT_TENANT, test=True):
            self.assertEqual(resolve_tenant_scope(), "demo")


class TestTenantDemoScopeLedger(unittest.TestCase):
    def setUp(self):
        os.environ["PYTEST_RUNNING"] = "1"
        os.environ["MONGODB_DB"] = TEST_DB_NAME
        os.environ["MULTI_TENANT_ENABLED"] = "1"
        os.environ["DEMO_MODE"] = "1"
        drop_database(test=True)
        store = MongoLedgerStore(test=True)
        store._collection("orders").replace_one(
            {"_id": "demo"},
            {
                "_id": "demo",
                "ledger_scope": "demo",
                "orders": [
                    {
                        "id": "op-buy-1",
                        "status": "filled",
                        "side": "buy",
                        "symbol": "BTC/USDT",
                        "timeframe": "4h",
                        "execution": {"price": 100.0, "amount": 1.0},
                        "timestamps": {"filled": "2026-01-01T00:00:00"},
                    }
                ],
            },
            upsert=True,
        )
        store._collection("positions").replace_one(
            {"_id": "demo"},
            {
                "_id": "demo",
                "ledger_scope": "demo",
                "positions": {
                    "BTC_USDT_4h": {
                        "amount": 1.0,
                        "average_entry": 100.0,
                        "peak_amount": 1.0,
                        "sold_percent": 0.0,
                    }
                },
            },
            upsert=True,
        )
        store._collection("positions").replace_one(
            {"_id": compound_ledger_id("henry", "demo")},
            {
                "_id": compound_ledger_id("henry", "demo"),
                "tenant_id": "henry",
                "ledger_scope": "demo",
                "positions": {},
            },
            upsert=True,
        )

    def tearDown(self):
        drop_database(test=True)
        os.environ.pop("MULTI_TENANT_ENABLED", None)
        os.environ.pop("DEMO_MODE", None)

    @patch("data_manager.get_config", return_value={"architecture": {"ledger_backend": "mongo"}})
    @patch("data_manager._ledger_reads_mongo", return_value=True)
    def test_operator_loads_demo_legacy_not_paper(self, _reads, _cfg):
        from data_manager import load_orders

        with tenant_context(DEFAULT_TENANT, scope="demo"):
            orders = load_orders("demo", tenant_id=DEFAULT_TENANT).get("orders", [])
            self.assertEqual(len(orders), 1)
            self.assertEqual(orders[0]["symbol"], "BTC/USDT")

        with tenant_context(DEFAULT_TENANT, scope="paper"):
            paper_orders = load_orders("paper", tenant_id=DEFAULT_TENANT).get("orders", [])
            self.assertEqual(paper_orders, [])

    @patch("data_manager.get_config", return_value={"architecture": {"ledger_backend": "mongo"}})
    @patch("data_manager._ledger_reads_mongo", return_value=True)
    def test_henry_demo_ledger_empty_isolated(self, _reads, _cfg):
        from data_manager import load_orders

        with tenant_context("henry", scope="demo"):
            orders = load_orders("demo", tenant_id="henry").get("orders", [])
            self.assertEqual(orders, [])

    @patch("data_manager.get_config", return_value={"architecture": {"ledger_backend": "mongo"}})
    @patch("data_manager._ledger_reads_mongo", return_value=True)
    def test_operator_portfolio_from_demo_scope(self, _reads, _cfg):
        with tenant_context(DEFAULT_TENANT, scope="demo"):
            load_positions(scope="demo", tenant_id=DEFAULT_TENANT)
            active = list_active_positions()
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]["symbol"], "BTC/USDT")

        with tenant_context("henry", scope="demo"):
            load_positions(scope="demo", tenant_id="henry")
            self.assertEqual(list_active_positions(), [])


if __name__ == "__main__":
    unittest.main()