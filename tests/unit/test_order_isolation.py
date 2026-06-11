import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.models import TradeOrder
from data_manager import load_orders, resolve_ledger_scope
from services.order_service import OrderService


class TestOrderIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.files = {
            "demo": os.path.join(self.tmp.name, "orders.demo.json"),
            "paper": os.path.join(self.tmp.name, "orders.paper.json"),
            "live": os.path.join(self.tmp.name, "orders.live.json"),
        }
        self.scope_patch = patch("data_manager.ORDERS_SCOPE_FILES", self.files)
        self.scope_patch.start()

    def tearDown(self):
        self.scope_patch.stop()

    def test_demo_mode_uses_demo_scope(self):
        with patch("data_manager.is_demo_mode", return_value=True):
            self.assertEqual(resolve_ledger_scope(), "demo")

    def test_paper_mode_uses_paper_scope(self):
        with patch("data_manager.is_demo_mode", return_value=False), \
             patch("data_manager.get_config", return_value={"trading_mode": "paper"}):
            self.assertEqual(resolve_ledger_scope(), "paper")

    def test_live_mode_uses_live_scope(self):
        with patch("data_manager.is_demo_mode", return_value=False), \
             patch("data_manager.get_config", return_value={"trading_mode": "live"}):
            self.assertEqual(resolve_ledger_scope(), "live")

    def test_orders_never_cross_scopes(self):
        for scope, symbol in [("demo", "DEMO/USDT"), ("paper", "PAPER/USDT"), ("live", "LIVE/USDT")]:
            OrderService(scope).create_from_request(
                TradeOrder("BUY", symbol, 1, 0, usdt_amount=10),
                telegram_token=f"{scope}_1",
            )
        for scope, symbol in [("demo", "DEMO/USDT"), ("paper", "PAPER/USDT"), ("live", "LIVE/USDT")]:
            data = load_orders(scope)
            self.assertEqual(len(data["orders"]), 1)
            self.assertEqual(data["orders"][0]["symbol"], symbol)
            self.assertEqual(data["ledger_scope"], scope)


if __name__ == "__main__":
    unittest.main()