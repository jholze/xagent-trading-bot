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

    def test_sell_list_chunks_long_message(self):
        active = [
            {
                "symbol": f"COIN{i}/USDT",
                "timeframe": "1h",
                "amount": 1000.0,
                "average_entry": 0.5,
            }
            for i in range(50)
        ]
        prices = {f"COIN{i}/USDT": 0.65 for i in range(50)}
        with patch("notifications.telegram_commands.trading_commands.list_active_positions", return_value=active), \
             patch("notifications.telegram_commands.trading_commands.get_prices_batch", return_value=prices), \
             patch("notifications.telegram_commands.trading_commands.send_telegram_message") as mock_send:
            self.assertTrue(trading_commands.handle("/sell"))
            self.assertGreater(mock_send.call_count, 1)
            for call in mock_send.call_args_list:
                self.assertLessEqual(len(call[0][0]), 4096)

    def test_sell_by_symbol_requests_confirmation(self):
        active = [
            {"symbol": "RAVE/USDT", "timeframe": "1h", "amount": 1000.0, "average_entry": 0.5},
        ]
        with patch("notifications.telegram_commands.trading_commands.list_active_positions", return_value=active), \
             patch("notifications.telegram_commands.trading_commands.get_prices_batch", return_value={"RAVE/USDT": 0.65}), \
             patch("notifications.telegram_commands.trading_commands.get_position", return_value={"amount": 1000.0}), \
             patch("notifications.telegram_commands.trading_commands.request_sell_confirmation") as mock_confirm:
            self.assertTrue(trading_commands.handle("/sell RAVE 30"))
            mock_confirm.assert_called_once()
            kwargs = mock_confirm.call_args.kwargs
            self.assertEqual(kwargs["symbol"], "RAVE/USDT")
            self.assertEqual(kwargs["timeframe"], "1h")
            self.assertAlmostEqual(kwargs["pct"], 0.3)
            self.assertAlmostEqual(kwargs["amount"], 300.0)

    def test_sell_1h_position_reads_1h_ledger_not_4h(self):
        """Regression: hardcoded 4h looked up empty ledger while real hold was on 1h."""
        active = [
            {"symbol": "RAVE/USDT", "timeframe": "1h", "amount": 500.0, "average_entry": 0.4},
        ]
        looked_up = []

        def _get_position(sym, tf):
            looked_up.append((sym, tf))
            if tf == "4h":
                return {"amount": 0.0}
            if tf == "1h":
                return {"amount": 500.0}
            return {"amount": 0.0}

        with patch("notifications.telegram_commands.trading_commands.list_active_positions", return_value=active), \
             patch("notifications.telegram_commands.trading_commands.get_prices_batch", return_value={"RAVE/USDT": 0.65}), \
             patch("notifications.telegram_commands.trading_commands.get_position", side_effect=_get_position), \
             patch("notifications.telegram_commands.trading_commands.request_sell_confirmation") as mock_confirm, \
             patch("notifications.telegram_commands.trading_commands.send_telegram_message"):
            self.assertTrue(trading_commands.handle("/sell RAVE 50"))

        self.assertEqual(looked_up, [("RAVE/USDT", "1h")])
        mock_confirm.assert_called_once()
        kwargs = mock_confirm.call_args.kwargs
        self.assertEqual(kwargs["timeframe"], "1h")
        self.assertAlmostEqual(kwargs["amount"], 250.0)

    def test_sell_by_number_uses_position_timeframe(self):
        active = [
            {"symbol": "SOL/USDT", "timeframe": "1h", "amount": 10.0, "average_entry": 100.0},
            {"symbol": "BTC/USDT", "timeframe": "4h", "amount": 0.1, "average_entry": 90000.0},
        ]
        prices = {"SOL/USDT": 150.0, "BTC/USDT": 95000.0}
        with patch("notifications.telegram_commands.trading_commands.list_active_positions", return_value=active), \
             patch("notifications.telegram_commands.trading_commands.get_prices_batch", return_value=prices), \
             patch("notifications.telegram_commands.trading_commands.get_position", return_value={"amount": 0.1}), \
             patch("notifications.telegram_commands.trading_commands.request_sell_confirmation") as mock_confirm:
            self.assertTrue(trading_commands.handle("/sell 1 50"))
            kwargs = mock_confirm.call_args.kwargs
            self.assertEqual(kwargs["symbol"], "BTC/USDT")
            self.assertEqual(kwargs["timeframe"], "4h")
            self.assertAlmostEqual(kwargs["amount"], 0.05)

    def test_sell_unknown_symbol_does_not_confirm(self):
        active = [{"symbol": "RAVE/USDT", "timeframe": "1h", "amount": 100.0, "average_entry": 0.5}]
        with patch("notifications.telegram_commands.trading_commands.list_active_positions", return_value=active), \
             patch("notifications.telegram_commands.trading_commands.get_prices_batch", return_value={"RAVE/USDT": 0.65}), \
             patch("notifications.telegram_commands.trading_commands.request_sell_confirmation") as mock_confirm, \
             patch("notifications.telegram_commands.trading_commands.send_telegram_message") as mock_send:
            self.assertTrue(trading_commands.handle("/sell PHANTOM 30"))
            mock_confirm.assert_not_called()
            self.assertIn("PHANTOM", mock_send.call_args[0][0])

    def test_sell_follow_up_symbol_via_context(self):
        import tempfile
        from pathlib import Path
        from notifications.telegram_commands import command_context as ctx

        tmp = tempfile.TemporaryDirectory()
        ctx._CONTEXT_FILE = Path(tmp.name) / "ctx.json"
        try:
            with patch("notifications.telegram_commands.trading_commands.list_active_positions") as mock_active, \
                 patch("notifications.telegram_commands.trading_commands.get_prices_batch", return_value={"RAVE/USDT": 0.65}), \
                 patch("notifications.telegram_commands.trading_commands.get_position", return_value={"amount": 200.0}), \
                 patch("notifications.telegram_commands.trading_commands.request_sell_confirmation") as mock_confirm:
                mock_active.return_value = [
                    {"symbol": "RAVE/USDT", "timeframe": "1h", "amount": 200.0, "average_entry": 0.5},
                ]
                ctx.set_context("99", "sell")
                self.assertTrue(ctx.try_resolve("99", "RAVE 25"))
                mock_confirm.assert_called_once()
                self.assertEqual(mock_confirm.call_args.kwargs["timeframe"], "1h")
        finally:
            tmp.cleanup()

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