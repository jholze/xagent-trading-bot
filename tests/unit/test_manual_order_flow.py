import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.models import RiskDecision, TradeOrder, TradeResult
from notifications.telegram_commands.manual_order_flow import (
    handle_callback,
    request_buy_confirmation,
    request_sell_confirmation,
)
from services.order_service import OrderService


class TestManualOrderFlow(unittest.TestCase):
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

    def tearDown(self):
        self.scope.stop()
        self.scope_patch.stop()

    def test_request_buy_shows_preview_buttons(self):
        trading = MagicMock()
        trading.refresh.return_value = trading
        trading.evaluate_risk.return_value = RiskDecision(
            approved=True,
            order=TradeOrder("BUY", "ARIA/USDT", 0.0325, 0, usdt_amount=200),
            drawdown_pct=1.0,
            size_multiplier=1.0,
        )
        trading.risk.status_summary.return_value = {
            "virtual_balance": 4800,
            "open_positions": 2,
            "max_open_positions": 10,
            "daily_trades": 1,
            "max_daily_trades": 8,
            "max_position_percent": 30,
            "drawdown_pct": 1.0,
            "drawdown_throttle_active": False,
            "base_usdt_per_trade": 25,
        }
        trading.config.risk_config = {"drawdown_throttle_pct": 10.0}

        with patch("notifications.telegram_commands.manual_order_flow.send_telegram_buttons") as mock_buttons:
            request_buy_confirmation(
                trading, symbol="ARIA/USDT", timeframe="4h", price=0.0325, usdt=200,
            )
            self.assertTrue(mock_buttons.called)
            msg = mock_buttons.call_args[0][0]
            buttons = mock_buttons.call_args[0][1]
            self.assertIn("Risiko-Prüfung", msg)
            self.assertIn("Trade-Cooldown", msg)
            orders, _ = OrderService("paper").list_orders()
            self.assertEqual(len(orders), 1)
            self.assertEqual(orders[0]["status"], "pending_confirmation")
            self.assertEqual(orders[0]["source"], "manual")
            self.assertIn("manual_ok:", buttons[0][0]["callback_data"])

    def test_request_buy_rejected_without_buttons(self):
        trading = MagicMock()
        trading.refresh.return_value = trading
        trading.evaluate_risk.return_value = RiskDecision(
            approved=False,
            message="Max open positions reached (5)",
            code="max_open_positions",
        )
        trading.risk.status_summary.return_value = {
            "open_positions": 5,
            "max_open_positions": 5,
            "daily_trades": 0,
            "max_daily_trades": 8,
        }

        with patch("notifications.telegram_commands.manual_order_flow.send_telegram_message") as mock_send, \
             patch("notifications.telegram_commands.manual_order_flow.send_telegram_buttons") as mock_buttons:
            request_buy_confirmation(
                trading, symbol="ARIA/USDT", timeframe="4h", price=0.0325, usdt=200,
            )
            mock_send.assert_called_once()
            mock_buttons.assert_not_called()
            self.assertIn("blockiert", mock_send.call_args[0][0])
            orders, _ = OrderService("paper").list_orders(status_filter={"rejected"})
            self.assertEqual(len(orders), 1)

    def test_confirm_executes_pending_buy(self):
        trading = MagicMock()
        trading.refresh.return_value = trading
        trading.execute_buy.return_value = TradeResult(True, "BUY", "ARIA/USDT", amount=100, price=0.0325, usdt_amount=200)

        svc = OrderService("paper")
        svc.create_from_request(
            TradeOrder("BUY", "ARIA/USDT", 0.0325, 0, usdt_amount=200),
            timeframe="4h",
            status="pending_confirmation",
            request_extra={"usdt": 200},
            telegram_token="abc123",
        )

        with patch("notifications.telegram_commands.manual_order_flow.TradingService", return_value=trading), \
             patch("price_fetcher.get_prices", return_value=(0.0325, 0.0325, None)), \
             patch("notifications.telegram_commands.manual_order_flow.answer_callback_query"):
            self.assertTrue(handle_callback({"id": "cb1", "data": "manual_ok:abc123"}))
            trading.execute_buy.assert_called_once_with("ARIA/USDT", "4h", 0.0325, 200, order_id="abc123")

    def test_request_sell_shows_preview_buttons(self):
        trading = MagicMock()
        trading.refresh.return_value = trading
        trading.evaluate_risk.return_value = RiskDecision(
            approved=True,
            order=TradeOrder("SELL", "SOL/USDT", 67.0, 2.5, signal="SELL"),
        )
        trading.risk.status_summary.return_value = {
            "virtual_balance": 4000,
            "open_positions": 2,
            "max_open_positions": 10,
            "daily_trades": 1,
            "max_daily_trades": 8,
            "max_position_percent": 30,
            "drawdown_pct": 0.0,
            "drawdown_throttle_active": False,
        }
        trading.config.risk_config = {"drawdown_throttle_pct": 10.0}

        with patch("notifications.telegram_commands.manual_order_flow.get_position", return_value={
            "amount": 5.0, "average_entry": 60.0,
        }), patch("notifications.telegram_commands.manual_order_flow.send_telegram_buttons") as mock_buttons:
            request_sell_confirmation(
                trading, symbol="SOL/USDT", timeframe="4h", price=67.0, amount=2.5, pct=0.5,
            )
            self.assertTrue(mock_buttons.called)
            self.assertIn("Verkauf", mock_buttons.call_args[0][0])

    def test_cancel_marks_cancelled(self):
        svc = OrderService("paper")
        svc.create_from_request(
            TradeOrder("BUY", "ARIA/USDT", 0.0325, 0, usdt_amount=200),
            timeframe="4h",
            status="pending_confirmation",
            request_extra={"usdt": 200},
            telegram_token="abc123",
        )
        with patch("notifications.telegram_commands.manual_order_flow.send_telegram_message") as mock_send, \
             patch("notifications.telegram_commands.manual_order_flow.answer_callback_query"):
            self.assertTrue(handle_callback({"id": "cb1", "data": "manual_no:abc123"}))
            self.assertEqual(svc.get_by_id("abc123")["status"], "cancelled")
            self.assertIn("abgebrochen", mock_send.call_args[0][0])


if __name__ == "__main__":
    unittest.main()