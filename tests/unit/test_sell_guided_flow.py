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
            ctx.set_context(
                "99", "sell", state="sell_awaiting_pct",
                position="RAVE", label="RAVE", timeframe="1h",
            )
            with patch("notifications.telegram_commands.trading_commands.request_sell_confirmation") as mock_confirm, \
                 patch("notifications.telegram_commands.trading_commands.send_telegram_buttons"):
                self.assertTrue(ctx.try_resolve("99", "50"))
                mock_confirm.assert_called_once()
                return mock_confirm.call_args.kwargs

        def _run_button():
            ctx.set_context(
                "99", "sell", state="sell_awaiting_pct",
                position="RAVE", label="RAVE", timeframe="1h",
            )
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

    def _two_longs(self):
        small = {
            "symbol": "SMALL/USDT",
            "timeframe": "1h",
            "amount": 10.0,
            "average_entry": 1.0,
        }
        big = {
            "symbol": "BIG/USDT",
            "timeframe": "4h",
            "amount": 100.0,
            "average_entry": 1.0,
        }
        prices = {"SMALL/USDT": 1.0, "BIG/USDT": 1.0}
        return [small, big], prices

    def _sellpos_query(self, ticker: str, timeframe: str, chat_id: str = "99"):
        return {
            "id": f"cb-pos-{ticker}-{timeframe}",
            "data": f"{trading_commands.SELL_POS_CALLBACK_PREFIX}{ticker}:{timeframe}",
            "message": {"chat": {"id": chat_id}},
        }

    def test_bare_sell_attaches_one_button_per_position_row(self):
        positions, prices = self._two_longs()
        with patch(
            "notifications.telegram_commands.trading_commands.list_active_positions",
            return_value=positions,
        ), patch(
            "notifications.telegram_commands.trading_commands.get_prices_batch",
            return_value=prices,
        ), patch(
            "notifications.telegram_commands.trading_commands.send_telegram_message",
        ) as mock_msg, patch(
            "notifications.telegram_commands.trading_commands.send_telegram_buttons",
        ) as mock_btn:
            self.assertTrue(trading_commands.handle("/sell"))
            mock_msg.assert_not_called()
            mock_btn.assert_called_once()
            _text, buttons = mock_btn.call_args[0][:2]
            self.assertTrue(all(len(row) == 1 for row in buttons))
            self.assertEqual(
                [row[0]["callback_data"] for row in buttons],
                ["sellpos:BIG:4h", "sellpos:SMALL:1h"],
            )
            self.assertIn("BIG", buttons[0][0]["text"])
            self.assertIn("SMALL", buttons[1][0]["text"])
            self.assertIn("🟡", buttons[0][0]["text"])
            self.assertIn("+0.0%", buttons[0][0]["text"])

    def test_bare_sell_keyboard_only_on_last_chunk(self):
        with patch(
            "notifications.telegram_commands.trading_commands.chunk_positions_message",
            return_value=["CHUNK-A", "CHUNK-B", "CHUNK-C"],
        ), patch(
            "notifications.telegram_commands.trading_commands.send_telegram_message",
        ) as mock_msg, patch(
            "notifications.telegram_commands.trading_commands.send_telegram_buttons",
        ) as mock_btn:
            self.assertTrue(trading_commands.handle("/sell"))
            self.assertEqual(
                [call.args[0] for call in mock_msg.call_args_list],
                ["CHUNK-A", "CHUNK-B"],
            )
            mock_btn.assert_called_once()
            self.assertEqual(mock_btn.call_args[0][0], "CHUNK-C")
            buttons = mock_btn.call_args[0][1]
            self.assertTrue(all(len(row) == 1 for row in buttons))
            self.assertEqual(
                [row[0]["callback_data"] for row in buttons],
                ["sellpos:RAVE:1h"],
            )

    def test_sellpos_tap_matches_typed_index(self):
        positions, prices = self._two_longs()
        pos_patches = [
            patch(
                "notifications.telegram_commands.trading_commands.list_active_positions",
                return_value=positions,
            ),
            patch(
                "notifications.telegram_commands.trading_commands.get_prices_batch",
                return_value=prices,
            ),
            patch(
                "notifications.telegram_commands.trading_commands.get_position",
                return_value={"amount": 100.0},
            ),
        ]
        for p in pos_patches:
            p.start()
            self.addCleanup(p.stop)

        def _meta_from_typed():
            ctx.clear_context("99")
            with patch(
                "notifications.telegram_commands.trading_commands.prompt_sell_percentage",
            ) as mock_prompt, patch(
                "notifications.telegram_commands.trading_commands.request_sell_confirmation",
            ) as mock_confirm, patch(
                "notifications.telegram_commands.trading_commands.send_telegram_buttons",
            ):
                self.assertTrue(trading_commands.handle("/sell 1"))
                mock_confirm.assert_not_called()
                return mock_prompt.call_args, dict(ctx.get_context("99")["meta"])

        def _meta_from_button():
            ctx.clear_context("99")
            ctx.set_context("99", "sell", state="sell_awaiting_position")
            with patch(
                "notifications.telegram_commands.trading_commands.prompt_sell_percentage",
            ) as mock_prompt, patch(
                "notifications.telegram_commands.trading_commands.request_sell_confirmation",
            ) as mock_confirm, patch(
                "notifications.telegram_commands.trading_commands.send_telegram_buttons",
            ), patch(
                "notifications.telegram_commands.trading_commands.answer_callback_query",
            ):
                self.assertTrue(trading_commands.handle_callback(self._sellpos_query("BIG", "4h")))
                mock_confirm.assert_not_called()
                return mock_prompt.call_args, dict(ctx.get_context("99")["meta"])

        typed_prompt, typed_meta = _meta_from_typed()
        button_prompt, button_meta = _meta_from_button()
        self.assertEqual(typed_prompt, button_prompt)
        self.assertEqual(typed_prompt.args[0], "BIG")
        self.assertEqual(typed_meta["state"], "sell_awaiting_pct")
        self.assertEqual(typed_meta["position"], "1")
        self.assertEqual(typed_meta["label"], "BIG")
        self.assertEqual(button_meta["state"], typed_meta["state"])
        self.assertEqual(button_meta["label"], typed_meta["label"])
        self.assertEqual(button_meta["position"], "BIG")
        self.assertEqual(button_meta["timeframe"], "4h")

    def test_sellpos_expired_does_not_prompt(self):
        expired = trading_commands._sell_menu_text("pct_expired")

        def _tap(setup):
            ctx.clear_context("99")
            setup()
            with patch(
                "notifications.telegram_commands.trading_commands.prompt_sell_percentage",
            ) as mock_prompt, patch(
                "notifications.telegram_commands.trading_commands.send_telegram_message",
            ) as mock_send, patch(
                "notifications.telegram_commands.trading_commands.answer_callback_query",
            ):
                self.assertTrue(trading_commands.handle_callback(self._sellpos_query("RAVE", "1h")))
                mock_prompt.assert_not_called()
                self.assertTrue(mock_send.called)
                self.assertEqual(mock_send.call_args[0][0], expired)

        with self.subTest("no_context"):
            _tap(lambda: None)

        with self.subTest("wrong_state"):
            _tap(lambda: ctx.set_context(
                "99", "sell", state="sell_awaiting_pct", position="RAVE", label="RAVE",
            ))

        with self.subTest("wrong_command"):
            _tap(lambda: ctx.set_context("99", "buy", default_usdt=25))

        with self.subTest("expired_ttl"):
            def _expire():
                ctx.set_context("99", "sell", state="sell_awaiting_position")
                store = ctx._load_store()
                old_ts = (datetime.now() - timedelta(minutes=20)).isoformat()
                store["contexts"]["99"]["updated_at"] = old_ts
                ctx._save_store(store)

            _tap(_expire)

    def test_handle_callback_routes_sellpos_prefix(self):
        query = self._sellpos_query("SMALL", "1h")
        with patch.object(
            trading_commands, "_handle_sell_pos_callback", return_value=True,
        ) as mock_pos, patch.object(
            trading_commands, "_handle_sell_pct_callback", return_value=True,
        ) as mock_pct:
            self.assertTrue(trading_commands.handle_callback(query))
            mock_pos.assert_called_once_with(query)
            mock_pct.assert_not_called()

        pct_query = {
            "id": "cb-pct",
            "data": "sellpct:50",
            "message": {"chat": {"id": "99"}},
        }
        with patch.object(
            trading_commands, "_handle_sell_pos_callback", return_value=True,
        ) as mock_pos, patch.object(
            trading_commands, "_handle_sell_pct_callback", return_value=True,
        ) as mock_pct:
            self.assertTrue(trading_commands.handle_callback(pct_query))
            mock_pct.assert_called_once_with(pct_query)
            mock_pos.assert_not_called()


if __name__ == "__main__":
    unittest.main()
