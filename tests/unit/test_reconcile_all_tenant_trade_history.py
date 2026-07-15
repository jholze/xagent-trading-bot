"""Regression: demo startup reconciles trade_history for all active tenants."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.models import TradeOrder, TradeResult
from core.tenant_context import tenant_context
from data_manager import reconcile_demo_trade_history_on_startup
from services.order_service import OrderService
from storage.mongo_client import TEST_DB_NAME, drop_database


class TestReconcileAllTenantTradeHistory(unittest.TestCase):
    def setUp(self):
        os.environ["PYTEST_RUNNING"] = "1"
        os.environ["MONGODB_DB"] = TEST_DB_NAME
        os.environ["MULTI_TENANT_ENABLED"] = "1"
        os.environ["DEMO_MODE"] = "1"
        drop_database(test=True)
        self.cfg = {"architecture": {"ledger_backend": "mongo"}, "trading_mode": "live", "live": {"dry_run": True, "simulated_balance_usdt": 100000}}

    def tearDown(self):
        drop_database(test=True)
        os.environ.pop("MULTI_TENANT_ENABLED", None)
        os.environ.pop("DEMO_MODE", None)

    @patch("data_manager.get_config")
    @patch("data_manager._ledger_reads_mongo", return_value=True)
    @patch("data_manager.is_demo_mode", return_value=True)
    @patch("storage.tenant_registry.list_active_tenants")
    def test_startup_reconcile_includes_henry(self, mock_list, _demo, _reads, mock_cfg):
        mock_cfg.return_value = self.cfg
        mock_list.return_value = [
            {
                "tenant_id": "henry",
                "status": "active",
                "telegram": {"owner_chat_id": "6512212782"},
            }
        ]

        with tenant_context("henry", scope="demo"):
            svc = OrderService()
            buy = TradeOrder("BUY", "SUI/USDT", 0.75, 0, usdt_amount=1000)
            created = svc.create_from_request(buy, status="executing")
            svc.link_execution_result(
                created["id"],
                TradeResult(True, "BUY", "SUI/USDT", amount=1333, price=0.75, usdt_amount=1000),
            )

        results = reconcile_demo_trade_history_on_startup(self.cfg)
        self.assertIn("default", results)
        self.assertIn("henry", results)
        self.assertAlmostEqual(float(results["henry"]["virtual_balance"]), 99000.0, places=0)


if __name__ == "__main__":
    unittest.main()