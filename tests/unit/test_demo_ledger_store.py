"""Demo scope ledger routing: Mongo-only at runtime."""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from data_manager import (
    compute_sim_cash_from_orders,
    load_live_trade_history,
    load_orders,
    load_trade_history,
    load_trade_history_document,
    reconcile_demo_trade_history_on_startup,
    resolve_ledger_backend,
)
from storage.ledger_router import MongoLedgerStoreAdapter, resolve_store
from strategies.positions import clear_positions_memory


class TestDemoMongoLedgerStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.orders_path = os.path.join(self.tmp.name, "orders.demo.json")
        self.cfg = {
            "trading_mode": "paper",
            "demo": {"backend": "mongo"},
            "architecture": {"ledger_backend": "mongo", "ledger_dual_write": False},
        }
        os.environ["DEMO_MODE"] = "1"
        os.environ["DEMO_LEDGER_BACKEND"] = "mongo"
        self.patches = [
            patch("data_manager.get_config", return_value=self.cfg),
            patch("data_manager.is_demo_mode", return_value=True),
            patch("data_manager.resolve_ledger_scope", return_value="demo"),
        ]
        for p in self.patches:
            p.start()
        from storage import ledger_router
        from services import order_service

        ledger_router._store_cache.clear()
        order_service._ORDERS_READ_CACHE.clear()
        clear_positions_memory()

    def tearDown(self):
        os.environ.pop("DEMO_LEDGER_BACKEND", None)
        for p in reversed(self.patches):
            p.stop()
        from storage import ledger_router
        from services import order_service

        ledger_router._store_cache.clear()
        order_service._ORDERS_READ_CACHE.clear()

    def test_resolve_ledger_backend_demo_defaults_mongo(self):
        self.assertEqual(resolve_ledger_backend("demo", self.cfg), "mongo")

    def test_resolve_store_demo_uses_mongo_adapter(self):
        store = resolve_store("demo", self.cfg)
        self.assertIsInstance(store, MongoLedgerStoreAdapter)

    def test_demo_cash_reconciled_from_mongo_orders_not_stale_history(self):
        filled_order = {
            "id": "d1",
            "status": "filled",
            "side": "buy",
            "symbol": "ARIA/USDT",
            "timeframe": "4h",
            "execution": {"price": 0.05, "amount": 1000},
            "timestamps": {"filled": "2026-06-01T10:00:00"},
        }
        stale_history = {
            "virtual_balance": 4000.0,
            "realized_pnl": 0.0,
            "open_positions": 0,
            "trades": [],
        }

        class FakeStore:
            def __init__(self):
                self.history = dict(stale_history)

            def load_orders(self, scope, tenant_id=None):
                return {
                    "ledger_scope": scope,
                    "orders": [filled_order],
                    "migrated_from_trades": True,
                }

            def load_trade_history(self, scope, tenant_id=None):
                return dict(self.history)

            def load_positions(self, scope, tenant_id=None):
                return {"ledger_scope": scope, "positions": {}}

            def save_trade_history(self, data, scope, tenant_id=None, **kwargs):
                self.history = dict(data)
                return True

        store = FakeStore()
        with patch("data_manager._mongo_ledger_store", return_value=store), patch(
            "core.tenant_context.multi_tenant_enabled", return_value=False
        ):
            history = load_trade_history_document("demo", self.cfg)
            reconciled = reconcile_demo_trade_history_on_startup(self.cfg)

        from core.portfolio_baseline import initial_capital

        buy_usdt = 1000 * 0.05
        expected_cash = initial_capital(scope="demo", config=self.cfg) - buy_usdt
        self.assertAlmostEqual(history["virtual_balance"], expected_cash, places=2)
        self.assertNotAlmostEqual(history["virtual_balance"], 4000.0, places=2)
        tenant_hist = reconciled.get("default") if isinstance(reconciled, dict) else None
        if not isinstance(tenant_hist, dict):
            tenant_hist = reconciled
        self.assertAlmostEqual(tenant_hist["virtual_balance"], expected_cash, places=2)

    def test_load_live_trade_history_in_demo_matches_order_reconciled_cash(self):
        filled_order = {
            "id": "d1",
            "status": "filled",
            "side": "buy",
            "symbol": "ARIA/USDT",
            "timeframe": "4h",
            "execution": {"price": 0.05, "amount": 1000},
            "timestamps": {"filled": "2026-06-01T10:00:00"},
        }

        class FakeStore:
            def __init__(self):
                self.history = {
                    "virtual_balance": 100000.0,
                    "realized_pnl": 0.0,
                    "trades": [],
                }

            def load_orders(self, scope, tenant_id=None):
                return {
                    "ledger_scope": scope,
                    "orders": [filled_order],
                    "migrated_from_trades": True,
                }

            def load_trade_history(self, scope, tenant_id=None):
                return dict(self.history)

            def load_positions(self, scope, tenant_id=None):
                return {"ledger_scope": scope, "positions": {}}

            def save_trade_history(self, data, scope, tenant_id=None, **kwargs):
                self.history = dict(data)
                return True

        with patch("data_manager._mongo_ledger_store", return_value=FakeStore()):
            from core.portfolio_baseline import initial_capital

            initial = initial_capital(scope="demo", config=self.cfg)
            expected_cash = compute_sim_cash_from_orders([filled_order], initial)
            live_hist = load_live_trade_history()
            scoped_hist = load_trade_history()
            orders_doc = load_orders("demo")

        self.assertEqual(len(orders_doc["orders"]), 1)
        self.assertAlmostEqual(live_hist["virtual_balance"], expected_cash, places=2)
        self.assertAlmostEqual(scoped_hist["virtual_balance"], expected_cash, places=2)
        self.assertNotAlmostEqual(live_hist["virtual_balance"], 100000.0, places=2)


if __name__ == "__main__":
    unittest.main()