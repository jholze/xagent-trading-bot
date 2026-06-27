"""Demo scope ledger routing: JSON orders SOT + Mongo positions cache."""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from data_manager import (
    load_orders,
    load_positions_document,
    load_trade_history_document,
    reconcile_demo_trade_history_on_startup,
    resolve_ledger_scope,
)
from storage.ledger_router import DemoLedgerStore, resolve_ledger_backend, resolve_store
from strategies.positions import bootstrap_positions, clear_positions_memory, count_open_positions


class TestDemoLedgerStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.orders_path = os.path.join(self.tmp.name, "orders.demo.json")
        self.positions_path = os.path.join(self.tmp.name, "positions.demo.json")
        self.orders_files = {
            "demo": self.orders_path,
            "paper": os.path.join(self.tmp.name, "orders.paper.json"),
            "live": os.path.join(self.tmp.name, "orders.live.json"),
        }
        self.positions_files = {
            "demo": self.positions_path,
            "paper": os.path.join(self.tmp.name, "positions.paper.json"),
            "live": os.path.join(self.tmp.name, "positions.live.json"),
        }
        with open(self.orders_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "ledger_scope": "demo",
                    "orders": [
                        {
                            "id": "d1",
                            "status": "filled",
                            "side": "buy",
                            "symbol": "ARIA/USDT",
                            "timeframe": "4h",
                            "execution": {"price": 0.05, "amount": 1000},
                            "timestamps": {"filled": "2026-06-01T10:00:00"},
                        }
                    ],
                    "migrated_from_trades": True,
                },
                f,
            )
        with open(self.positions_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "ledger_scope": "demo",
                    "positions": {
                        "ARIA_USDT_4h": {
                            "amount": 1000.0,
                            "peak_amount": 1000.0,
                            "strategy_tier": "stable",
                        }
                    },
                },
                f,
            )
        self.cfg = {
            "trading_mode": "paper",
            "architecture": {"ledger_backend": "mongo", "ledger_dual_write": False},
        }
        self.patches = [
            patch("data_manager.ORDERS_SCOPE_FILES", self.orders_files),
            patch("data_manager.POSITIONS_SCOPE_FILES", self.positions_files),
            patch("storage.ledger_router.ORDERS_SCOPE_FILES", self.orders_files),
            patch("storage.ledger_router.POSITIONS_SCOPE_FILES", self.positions_files),
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
        for p in reversed(self.patches):
            p.stop()
        from storage import ledger_router
        from services import order_service

        ledger_router._store_cache.clear()
        order_service._ORDERS_READ_CACHE.clear()

    def test_resolve_ledger_backend_demo_is_hybrid(self):
        self.assertEqual(resolve_ledger_backend("demo", self.cfg), "demo_hybrid")

    def test_demo_store_reads_orders_from_json_not_empty_mongo(self):
        store = resolve_store("demo", self.cfg)
        self.assertIsInstance(store, DemoLedgerStore)

        class EmptyMongo:
            def load_orders(self, scope):
                return {"ledger_scope": scope, "orders": [], "migrated_from_trades": False}

            def load_positions(self, scope):
                return {"ledger_scope": scope, "positions": {}}

        with patch.object(store, "_mongo") as mock_mongo:
            mock_mongo.load_orders.side_effect = EmptyMongo().load_orders
            mock_mongo.load_positions.side_effect = EmptyMongo().load_positions
            orders = store.load_orders("demo")
            self.assertEqual(len(orders["orders"]), 1)
            self.assertEqual(orders["orders"][0]["symbol"], "ARIA/USDT")

    def test_bootstrap_positions_derives_from_demo_json_orders(self):
        bootstrap_positions(scope="demo")
        self.assertGreater(count_open_positions(), 0)
        self.assertEqual(load_orders("demo")["orders"][0]["status"], "filled")

    def test_load_positions_document_prefers_mongo_cache_when_populated(self):
        store = resolve_store("demo", self.cfg)
        mongo_cache = {
            "ledger_scope": "demo",
            "positions": {
                "CACHE_USDT_4h": {"amount": 5.0, "peak_amount": 5.0, "strategy_tier": "volatile"},
            },
        }
        with patch.object(store._mongo, "load_positions", return_value=mongo_cache):
            doc = store.load_positions("demo")
        self.assertIn("CACHE_USDT_4h", doc["positions"])

    def test_save_positions_writes_json_only_not_mongo(self):
        store = resolve_store("demo", self.cfg)
        payload = {"ledger_scope": "demo", "positions": {"X_USDT_4h": {"amount": 1.0}}}
        with patch.object(store._mongo, "save_positions") as mock_mongo_save:
            self.assertTrue(store.save_positions(payload, "demo"))
            mock_mongo_save.assert_not_called()

    def test_demo_cash_reconciled_from_json_orders_not_stale_mongo(self):
        history_path = os.path.join(self.tmp.name, "live_trade_history.demo.json")
        with open(history_path, "w", encoding="utf-8") as f:
            json.dump(
                {"virtual_balance": 4000.0, "realized_pnl": 0.0, "open_positions": 0, "trades": []},
                f,
            )
        with patch("data_manager.LIVE_TRADE_HISTORY_FILE", "live_trade_history.demo.json"), \
             patch("data_manager.get_data_file", side_effect=lambda name: history_path if "live_trade" in name else self.orders_path):
            history = load_trade_history_document("demo", self.cfg)
        from core.portfolio_baseline import initial_capital

        buy_usdt = 1000 * 0.05
        expected_cash = initial_capital(scope="demo", config=self.cfg) - buy_usdt
        self.assertAlmostEqual(history["virtual_balance"], expected_cash, places=2)
        self.assertNotAlmostEqual(history["virtual_balance"], 4000.0, places=2)
        self.assertEqual(reconcile_demo_trade_history_on_startup(self.cfg)["virtual_balance"], history["virtual_balance"])


if __name__ == "__main__":
    unittest.main()