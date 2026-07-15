"""Regression: satellite tenants see their own demo orders in /orders."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.models import TradeOrder, TradeResult
from core.tenant_context import tenant_context
from core.tenant_routing import resolve_incoming_tenant
from services.order_service import OrderService
from storage.mongo_client import TEST_DB_NAME, drop_database
from storage.mongo_ledger import MongoLedgerStore


class TestHenryTenantOrders(unittest.TestCase):
    def setUp(self):
        os.environ["PYTEST_RUNNING"] = "1"
        os.environ["MONGODB_DB"] = TEST_DB_NAME
        os.environ["MULTI_TENANT_ENABLED"] = "1"
        os.environ["DEMO_MODE"] = "1"
        os.environ["TELEGRAM_CHAT_ID"] = "111"
        drop_database(test=True)
        self.store = MongoLedgerStore(test=True)
        self.cfg = {"architecture": {"ledger_backend": "mongo"}, "trading_mode": "live"}

    def tearDown(self):
        drop_database(test=True)
        os.environ.pop("MULTI_TENANT_ENABLED", None)
        os.environ.pop("DEMO_MODE", None)
        os.environ.pop("TELEGRAM_CHAT_ID", None)

    @patch("data_manager.get_config", return_value={"architecture": {"ledger_backend": "mongo"}})
    @patch("data_manager._ledger_reads_mongo", return_value=True)
    @patch("core.tenant_routing.multi_tenant_enabled", return_value=True)
    @patch("storage.tenant_registry.find_tenant_by_owner_chat_id")
    @patch("data_manager.is_demo_mode", return_value=True)
    @patch("data_manager.resolve_ledger_scope", return_value="demo")
    def test_henry_sees_own_filled_order_not_operator_book(
        self, _scope, _demo, mock_find, _mt, _reads, _cfg
    ):
        mock_find.return_value = {
            "tenant_id": "henry",
            "defaults": {"ledger_scope": "paper"},
            "telegram": {"owner_chat_id": "6512212782"},
        }
        route = resolve_incoming_tenant(chat_id="6512212782")
        self.assertEqual(route.tenant_id, "henry")
        self.assertEqual(route.scope, "demo")

        svc_default = OrderService()
        with tenant_context("default", scope="demo"):
            buy = TradeOrder("BUY", "BTC/USDT", 100.0, 0, usdt_amount=500)
            created = svc_default.create_from_request(buy, status="executing")
            svc_default.link_execution_result(
                created["id"],
                TradeResult(True, "BUY", "BTC/USDT", amount=5, price=100, usdt_amount=500),
            )

        with tenant_context("henry", scope=route.scope, owner_chat_id="6512212782"):
            svc_henry = OrderService()
            buy = TradeOrder("BUY", "SUI/USDT", 0.75, 0, usdt_amount=300)
            created = svc_henry.create_from_request(buy, status="executing")
            svc_henry.link_execution_result(
                created["id"],
                TradeResult(True, "BUY", "SUI/USDT", amount=400, price=0.75, usdt_amount=300),
            )
            book, pages = svc_henry.list_orders(trade_book_only=True)
            self.assertEqual(len(book), 1)
            self.assertEqual(book[0]["symbol"], "SUI/USDT")

        with tenant_context("default", scope="demo"):
            op_book, _ = OrderService().list_orders(trade_book_only=True)
            self.assertEqual(len(op_book), 1)
            self.assertEqual(op_book[0]["symbol"], "BTC/USDT")

    @patch("data_manager.get_config")
    @patch("data_manager._ledger_reads_mongo", return_value=True)
    def test_list_recent_rejected(self, _reads, mock_cfg):
        mock_cfg.return_value = self.cfg
        with tenant_context("default", scope="demo"):
            svc = OrderService()
            buy = TradeOrder("BUY", "ARIA/USDT", 0.05, 0, usdt_amount=100)
            svc.create_from_request(buy, status="executing", telegram_token="f1")
            svc.link_execution_result(
                "f1",
                TradeResult(True, "BUY", "ARIA/USDT", amount=2000, price=0.05, usdt_amount=100),
            )
            from core.models import RiskDecision

            reject = TradeOrder("BUY", "SOL/USDT", 70, 2, signal="BUY")
            svc.record_rejected(
                reject,
                RiskDecision(approved=False, message="Cooldown", code="trade_cooldown", order=reject),
            )
            blocked = svc.list_recent_rejected(limit=5)
            self.assertEqual(len(blocked), 1)
            self.assertEqual(blocked[0]["status"], "rejected")


if __name__ == "__main__":
    unittest.main()