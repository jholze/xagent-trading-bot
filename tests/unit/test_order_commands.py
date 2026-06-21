import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.models import TradeOrder
from notifications.telegram_commands import order_commands
from services.order_service import OrderService


class TestOrderCommands(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.scope_patch = patch("data_manager.ORDERS_SCOPE_FILES", {
            "demo": os.path.join(self.tmp.name, "orders.demo.json"),
            "paper": os.path.join(self.tmp.name, "orders.paper.json"),
            "live": os.path.join(self.tmp.name, "orders.live.json"),
        })
        self.scope_patch.start()
        self.scope = patch("services.order_service.resolve_ledger_scope", return_value="paper")
        self.scope.start()
        svc = OrderService("paper")
        svc.create_from_request(
            TradeOrder("BUY", "ARIA/USDT", 0.05, 0, usdt_amount=100),
            telegram_token="t1",
        )
        svc.create_from_request(
            TradeOrder("SELL", "SOL/USDT", 70, 2, signal="SELL"),
            status="filled",
            telegram_token="t2",
        )
        svc.update_status("t2", "filled", execution={"usdt": 140, "price": 70, "amount": 2})

    def tearDown(self):
        self.scope.stop()
        self.scope_patch.stop()

    def test_orders_lists_page(self):
        with patch("notifications.telegram_commands.order_commands.send_telegram_message") as mock_send:
            self.assertTrue(order_commands.handle("/orders"))
            msg = mock_send.call_args[0][0]
            self.assertIn("Orderbuch", msg)
            self.assertIn("PAPER", msg)
            self.assertIn("SOL", msg)
            self.assertNotIn("PENDING_CONFIRMATION", msg)

    def test_orders_detail_by_number(self):
        with patch("notifications.telegram_commands.order_commands.send_telegram_message") as mock_send:
            self.assertTrue(order_commands.handle("/orders 1"))
            msg = mock_send.call_args[0][0]
            self.assertIn("Order #1", msg)
            self.assertIn("ARIA", msg)

    def test_orders_page_command(self):
        with patch("notifications.telegram_commands.order_commands.send_telegram_message") as mock_send:
            self.assertTrue(order_commands.handle("/orders page 1"))
            self.assertIn("Seite", mock_send.call_args[0][0])

    def test_orders_invalid_shows_hint(self):
        with patch("notifications.telegram_commands.order_commands.send_telegram_message") as mock_send:
            self.assertTrue(order_commands.handle("/orders abc"))
            self.assertIn("/orders", mock_send.call_args[0][0])

    def test_pagination_callback(self):
        with patch("notifications.telegram_commands.order_commands.send_telegram_message") as mock_send, \
             patch("notifications.telegram_commands.order_commands.answer_callback_query"):
            self.assertTrue(order_commands.handle_callback({
                "id": "cb1", "data": "orders_page:paper:1",
            }))
            self.assertIn("Orderbuch", mock_send.call_args[0][0])

    def test_router_dispatches_orders_callback(self):
        from notifications.telegram_commands.router import dispatch_callback

        with patch("notifications.telegram_commands.trading_commands.handle_callback", return_value=False), \
             patch("notifications.telegram_commands.order_commands.handle_callback", return_value=True) as mock_orders, \
             patch("notifications.telegram_commands.x_commands.handle_callback") as mock_x:
            self.assertTrue(dispatch_callback({"id": "1", "data": "orders_page:paper:1"}))
            mock_orders.assert_called_once()
            mock_x.assert_not_called()


if __name__ == "__main__":
    unittest.main()