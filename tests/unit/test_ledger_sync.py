import os
import sys
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from data_manager import load_orders, resolve_ledger_scope, resolve_positions_file
from services.ledger_sync import (
    count_open_positions_from_orders,
    on_trading_mode_change,
    rebuild_positions_from_orders,
    sync_positions_on_startup,
)
from services.order_service import OrderService
from strategies.positions import count_open_positions, get_active_scope, get_position, positions


class TestLedgerSync(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.orders_files = {
            "demo": os.path.join(self.tmp.name, "orders.demo.json"),
            "paper": os.path.join(self.tmp.name, "orders.paper.json"),
            "live": os.path.join(self.tmp.name, "orders.live.json"),
        }
        self.positions_files = {
            "demo": os.path.join(self.tmp.name, "positions.demo.json"),
            "paper": os.path.join(self.tmp.name, "positions.paper.json"),
            "live": os.path.join(self.tmp.name, "positions.live.json"),
        }
        self._positions_backup = {
            k: {**v, "amount": Decimal(str(v["amount"]))} for k, v in positions.items()
        }
        positions.clear()

        self.orders_patch = patch("data_manager.ORDERS_SCOPE_FILES", self.orders_files)
        self.positions_patch = patch("data_manager.POSITIONS_SCOPE_FILES", self.positions_files)
        self.orders_patch.start()
        self.positions_patch.start()

    def tearDown(self):
        self.positions_patch.stop()
        self.orders_patch.stop()
        positions.clear()
        positions.update(self._positions_backup)

    def _filled_buy(self, scope, symbol, price, amount):
        from core.models import TradeOrder

        svc = OrderService(scope)
        order = svc.create_from_request(
            TradeOrder("BUY", symbol, price, amount, usdt_amount=price * amount),
            telegram_token=f"{scope}_buy",
        )
        svc.update_status(
            order["id"],
            "filled",
            execution={"price": price, "amount": amount, "usdt": price * amount},
        )

    def _filled_sell(self, scope, symbol, price, amount):
        from core.models import TradeOrder

        svc = OrderService(scope)
        order = svc.create_from_request(
            TradeOrder("SELL", symbol, price, amount, signal="SELL_FULL"),
            telegram_token=f"{scope}_sell",
        )
        svc.update_status(
            order["id"],
            "filled",
            execution={"price": price, "amount": amount, "usdt": price * amount},
            pnl=-1.0,
        )

    def test_live_scope_does_not_inherit_paper_positions(self):
        self._filled_buy("paper", "ARIA/USDT", 0.05, 1000)
        self._filled_buy("live", "SOL/USDT", 60.0, 2.0)

        with patch("data_manager.is_demo_mode", return_value=False), \
             patch("data_manager.get_config", return_value={"trading_mode": "paper"}):
            rebuild_positions_from_orders("paper")
            self.assertEqual(get_active_scope(), "paper")
            self.assertGreater(float(get_position("ARIA/USDT", "4h")["amount"]), 0)

        with patch("data_manager.is_demo_mode", return_value=False), \
             patch("data_manager.get_config", return_value={"trading_mode": "live"}):
            rebuild_positions_from_orders("live")
            self.assertEqual(get_active_scope(), "live")
            self.assertEqual(float(get_position("ARIA/USDT", "4h")["amount"]), 0)
            self.assertGreater(float(get_position("SOL/USDT", "4h")["amount"]), 0)

    def test_mode_switch_rebuilds_target_ledger(self):
        self._filled_buy("paper", "HIGH/USDT", 0.08, 500)
        self._filled_sell("live", "HIGH/USDT", 0.05, 500)

        with patch("data_manager.is_demo_mode", return_value=False), \
             patch("data_manager.get_config", return_value={"trading_mode": "live"}):
            msg = on_trading_mode_change("paper", "live")

        self.assertEqual(get_active_scope(), "live")
        self.assertEqual(float(get_position("HIGH/USDT", "4h")["amount"]), 0)
        self.assertIn("LIVE", msg)

    def test_resolve_positions_file_scopes(self):
        with patch("data_manager.is_demo_mode", return_value=False):
            self.assertEqual(resolve_positions_file("paper"), self.positions_files["paper"])
            self.assertEqual(resolve_positions_file("live"), self.positions_files["live"])

    def test_demo_mode_uses_demo_scope(self):
        with patch("data_manager.is_demo_mode", return_value=True):
            self.assertEqual(resolve_ledger_scope(), "demo")

    def test_partial_sell_peak_amount_from_orders(self):
        self._filled_buy("paper", "XPL/USDT", 1.0, 100.0)
        self._filled_sell("paper", "XPL/USDT", 1.2, 30.0)

        rebuild_positions_from_orders("paper")
        pos = get_position("XPL/USDT", "4h")
        self.assertAlmostEqual(float(pos["amount"]), 70.0, places=2)
        self.assertAlmostEqual(float(pos["peak_amount"]), 100.0, places=2)
        self.assertAlmostEqual(pos["sold_percent"], 0.3, places=2)

    def test_sync_positions_on_startup_rebuilds_on_drift(self):
        self._filled_buy("live", "ARIA/USDT", 0.05, 1000)
        with open(self.positions_files["live"], "w", encoding="utf-8") as f:
            f.write('{"positions": {}, "ledger_scope": "live"}')

        with patch("data_manager.is_demo_mode", return_value=False), \
             patch("data_manager.get_config", return_value={"trading_mode": "live"}), \
             patch("services.ledger_sync.migrate_legacy_positions"):
            sync_positions_on_startup()

        self.assertEqual(count_open_positions_from_orders("live"), count_open_positions())
        self.assertGreater(float(get_position("ARIA/USDT", "4h")["amount"]), 0)


if __name__ == "__main__":
    unittest.main()