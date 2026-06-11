import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from notifications.telegram_commands import trading_commands


class TestTradingCommands(unittest.TestCase):
    def test_buy_lists_coins_without_args(self):
        with patch("notifications.telegram_commands.trading_commands.list_coins") as mock_coins, \
             patch("notifications.telegram_commands.trading_commands.get_prices_batch", return_value={"ARIA/USDT": 0.05}), \
             patch("notifications.telegram_commands.trading_commands.send_telegram_message") as mock_send:
            mock_coins.return_value = [{"symbol": "ARIA/USDT", "name": "Aria", "active": True}]
            self.assertTrue(trading_commands.handle("/buy"))
            self.assertIn("Coins kaufen", mock_send.call_args[0][0])

    def test_buy_with_args_requests_confirmation(self):
        with patch("notifications.telegram_commands.trading_commands.list_coins") as mock_coins, \
             patch("notifications.telegram_commands.trading_commands.get_prices", return_value=(0.05, 0.05, None)), \
             patch("notifications.telegram_commands.trading_commands.request_buy_confirmation") as mock_confirm:
            mock_coins.return_value = [{"symbol": "ARIA/USDT", "active": True}]
            self.assertTrue(trading_commands.handle("/buy 1 200"))
            mock_confirm.assert_called_once()

    def test_callback_delegates_to_manual_flow(self):
        with patch("notifications.telegram_commands.manual_order_flow.handle_callback", return_value=True) as mock_cb:
            self.assertTrue(trading_commands.handle_callback({"data": "manual_ok:abc"}))
            mock_cb.assert_called_once()


    def test_router_dispatches_manual_callback(self):
        from notifications.telegram_commands.router import dispatch_callback

        with patch("notifications.telegram_commands.trading_commands.handle_callback", return_value=True) as mock_trade, \
             patch("notifications.telegram_commands.x_commands.handle_callback") as mock_x:
            self.assertTrue(dispatch_callback({"id": "1", "data": "manual_ok:abc"}))
            mock_trade.assert_called_once()
            mock_x.assert_not_called()


if __name__ == "__main__":
    unittest.main()