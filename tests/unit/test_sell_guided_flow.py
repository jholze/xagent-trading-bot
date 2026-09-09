import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from notifications.telegram_commands import command_context as ctx
from notifications.telegram_commands import trading_commands


class TestSellGuidedFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        ctx._CONTEXT_FILE = Path(self.tmp.name) / "ctx.json"
        ctx.set_chat_id("99")
        self.position = {
            "symbol": "RAVE/USDT",
            "timeframe": "1h",
            "amount": 200.0,
            "average_entry": 0.5,
        }
        self.patches = [
            patch(
                "notifications.telegram_commands.trading_commands.list_active_positions",
                return_value=[self.position],
            ),
            patch(
                "notifications.telegram_commands.trading_commands.get_prices_batch",
                return_value={"RAVE/USDT": 0.65},
            ),
            patch(
                "notifications.telegram_commands.trading_commands.get_prices",
                return_value=(0.65, 0.65, None),
            ),
            patch(
                "notifications.telegram_commands.trading_commands.get_position",
                return_value={"amount": 200.0},
            ),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        ctx.clear_context("99")
        ctx.set_chat_id("")
        self.tmp.cleanup()

    def test_bare_sell_sets_awaiting_position_state(self):
        with patch("notifications.telegram_commands.trading_commands.send_telegram_message"), \
             patch("notifications.telegram_commands.trading_commands.request_sell_confirmation") as mock_confirm:
            self.assertTrue(trading_commands.handle("/sell"))
            mock_confirm.assert_not_called()
        entry = ctx.get_context("99")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["command"], "sell")
        self.assertEqual(entry["meta"]["state"], "sell_awaiting_position")

    def test_direct_one_arg_asks_percent_instead_of_defaulting(self):
        with patch("notifications.telegram_commands.trading_commands.send_telegram_buttons") as mock_btn, \
             patch("notifications.telegram_commands.trading_commands.request_sell_confirmation") as mock_confirm:
            self.assertTrue(trading_commands.handle("/sell RAVE"))
            mock_confirm.assert_not_called()
            self.assertTrue(mock_btn.called)
            prompt, buttons = mock_btn.call_args[0][:2]
            self.assertIn("RAVE", prompt)
            callback_data = [btn["callback_data"] for row in buttons for btn in row]
            self.assertEqual(callback_data, ["sellpct:25", "sellpct:50", "sellpct:75", "sellpct:100"])
        entry = ctx.get_context("99")
        self.assertEqual(entry["meta"]["state"], "sell_awaiting_pct")
        self.assertEqual(entry["meta"]["position"], "RAVE")

    def test_position_step_to_percent_step_refreshes_ttl(self):
        ctx.set_context("99", "sell", state="sell_awaiting_position")
        store = ctx._load_store()
        old_ts = (datetime.now() - timedelta(minutes=10)).isoformat()
        store["contexts"]["99"]["updated_at"] = old_ts
        ctx._save_store(store)

        with patch("notifications.telegram_commands.trading_commands.send_telegram_buttons") as mock_btn, \
             patch("notifications.telegram_commands.trading_commands.request_sell_confirmation") as mock_confirm:
            self.assertTrue(ctx.try_resolve("99", "RAVE"))
            mock_confirm.assert_not_called()
            self.assertTrue(mock_btn.called)

        entry = ctx.get_context("99")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["meta"]["state"], "sell_awaiting_pct")
        self.assertEqual(entry["meta"]["position"], "RAVE")
        self.assertGreater(entry["updated_at"], old_ts)

    def test_typed_percent_and_quick_button_produce_same_confirmation(self):
        def _run_typed():
            ctx.set_context("99", "sell", state="sell_awaiting_pct", position="RAVE", label="RAVE")
            with patch("notifications.telegram_commands.trading_commands.request_sell_confirmation") as mock_confirm, \
                 patch("notifications.telegram_commands.trading_commands.send_telegram_buttons"):
                self.assertTrue(ctx.try_resolve("99", "50"))
                mock_confirm.assert_called_once()
                return mock_confirm.call_args.kwargs

        def _run_button():
            ctx.set_context("99", "sell", state="sell_awaiting_pct", position="RAVE", label="RAVE")
            with patch("notifications.telegram_commands.trading_commands.request_sell_confirmation") as mock_confirm, \
                 patch("notifications.telegram_commands.trading_commands.send_telegram_buttons"), \
                 patch("notifications.telegram_commands.trading_commands.answer_callback_query"):
                self.assertTrue(trading_commands.handle_callback({
                    "id": "cb1",
                    "data": "sellpct:50",
                    "message": {"chat": {"id": "99"}},
                }))
                mock_confirm.assert_called_once()
                return mock_confirm.call_args.kwargs

        typed_kwargs = _run_typed()
        button_kwargs = _run_button()
        self.assertEqual(typed_kwargs["symbol"], button_kwargs["symbol"])
        self.assertEqual(typed_kwargs["timeframe"], button_kwargs["timeframe"])
        self.assertAlmostEqual(typed_kwargs["pct"], 0.5)
        self.assertAlmostEqual(button_kwargs["pct"], 0.5)
        self.assertAlmostEqual(typed_kwargs["amount"], button_kwargs["amount"])
        self.assertIsNone(ctx.get_context("99"))

    def test_invalid_percent_reply_reasks_instead_of_defaulting(self):
        ctx.set_context("99", "sell", state="sell_awaiting_pct", position="RAVE", label="RAVE")
        with patch("notifications.telegram_commands.trading_commands.send_telegram_buttons") as mock_btn, \
             patch("notifications.telegram_commands.trading_commands.request_sell_confirmation") as mock_confirm:
            self.assertTrue(ctx.try_resolve("99", "nope"))
            mock_confirm.assert_not_called()
            self.assertTrue(mock_btn.called)
            prompt = mock_btn.call_args[0][0]
            self.assertIn("1", prompt)
            self.assertIn("100", prompt)
        entry = ctx.get_context("99")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["meta"]["state"], "sell_awaiting_pct")
        self.assertEqual(entry["meta"]["position"], "RAVE")

    def test_invalid_percent_button_reasks_instead_of_defaulting(self):
        ctx.set_context("99", "sell", state="sell_awaiting_pct", position="RAVE", label="RAVE")
        with patch("notifications.telegram_commands.trading_commands.send_telegram_buttons") as mock_btn, \
             patch("notifications.telegram_commands.trading_commands.request_sell_confirmation") as mock_confirm, \
             patch("notifications.telegram_commands.trading_commands.answer_callback_query"):
            self.assertTrue(trading_commands.handle_callback({
                "id": "cb2",
                "data": "sellpct:0",
                "message": {"chat": {"id": "99"}},
            }))
            mock_confirm.assert_not_called()
            self.assertTrue(mock_btn.called)
        entry = ctx.get_context("99")
        self.assertEqual(entry["meta"]["state"], "sell_awaiting_pct")

    def test_power_user_combined_reply_skips_percent_step(self):
        ctx.set_context("99", "sell", state="sell_awaiting_position")
        with patch("notifications.telegram_commands.trading_commands.send_telegram_buttons") as mock_btn, \
             patch("notifications.telegram_commands.trading_commands.request_sell_confirmation") as mock_confirm:
            self.assertTrue(ctx.try_resolve("99", "RAVE 30"))
            mock_btn.assert_not_called()
            mock_confirm.assert_called_once()
            self.assertAlmostEqual(mock_confirm.call_args.kwargs["pct"], 0.3)
        self.assertIsNone(ctx.get_context("99"))


if __name__ == "__main__":
    unittest.main()
