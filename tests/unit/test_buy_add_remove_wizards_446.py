"""#446 — tap-first /buy /add /remove wizards (sell-wizard shape).

Nothing here writes into ``data/``: command context lives in a tmp file.
Frozen sell-wizard assertions in ``test_sell_guided_flow.py`` stay untouched.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from notifications.telegram_commands import command_context as ctx
from notifications.telegram_commands import trading_commands, watchlist_commands
from notifications.telegram_commands.menu_i18n import reload_menu_data, set_user_language
from notifications.telegram_i18n import reload_messages

TC = "notifications.telegram_commands.trading_commands"
WL = "notifications.telegram_commands.watchlist_commands"
CHAT = "446"


def _buycoin_query(index: int, chat_id: str = CHAT) -> dict:
    return {
        "id": f"cb-buycoin-{index}",
        "data": f"{trading_commands.BUY_COIN_CALLBACK_PREFIX}{index}",
        "message": {"chat": {"id": chat_id}},
    }


def _buyamt_query(usdt: int, chat_id: str = CHAT) -> dict:
    return {
        "id": f"cb-buyamt-{usdt}",
        "data": f"{trading_commands.BUY_AMT_CALLBACK_PREFIX}{usdt}",
        "message": {"chat": {"id": chat_id}},
    }


class TestBuyAddRemoveWizards446(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        ctx._CONTEXT_FILE = Path(self.tmp.name) / "ctx.json"
        ctx.set_chat_id(CHAT)
        set_user_language("de")
        reload_messages()
        reload_menu_data()
        self.coins = [
            {"symbol": "ARIA/USDT", "name": "Aria", "active": True},
            {"symbol": "SOL/USDT", "name": "Solana", "active": True},
        ]
        self.cfg = MagicMock()
        self.cfg.max_usdt_per_trade = 25
        self.cfg.raw = {}
        self.patches = [
            patch(f"{TC}.list_coins", return_value=self.coins),
            patch(f"{TC}.get_prices_batch", return_value={"ARIA/USDT": 0.05, "SOL/USDT": 145.0}),
            patch(f"{TC}.get_prices", return_value=(0.05, 0.05, None)),
            patch(f"{TC}.get_bot_config", return_value=self.cfg),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        ctx.clear_context(CHAT)
        ctx.set_chat_id("")
        self.tmp.cleanup()

    def test_bare_buy_sets_awaiting_coin_and_does_not_confirm(self):
        with patch(f"{TC}.send_telegram_buttons") as mock_btn, \
             patch(f"{TC}.request_buy_confirmation") as mock_confirm:
            self.assertTrue(trading_commands.handle("/buy"))
            mock_confirm.assert_not_called()
            buttons = mock_btn.call_args[0][1]
            self.assertEqual(
                [row[0]["callback_data"] for row in buttons],
                ["buycoin:1", "buycoin:2"],
            )
            self.assertTrue(all(not cb.startswith("sellpos:") and not cb.startswith("sellpct:")
                                for row in buttons for btn in row for cb in [btn["callback_data"]]))
        entry = ctx.get_context(CHAT)
        self.assertEqual(entry["command"], "buy")
        self.assertEqual(entry["meta"]["state"], "buy_awaiting_coin")

    def test_one_arg_buy_asks_amount_instead_of_defaulting(self):
        with patch(f"{TC}.send_telegram_buttons") as mock_btn, \
             patch(f"{TC}.request_buy_confirmation") as mock_confirm:
            self.assertTrue(trading_commands.handle("/buy ARIA"))
            mock_confirm.assert_not_called()
            prompt, buttons = mock_btn.call_args[0][:2]
            self.assertIn("ARIA", prompt)
            callbacks = [btn["callback_data"] for row in buttons for btn in row]
            self.assertIn("buyamt:10", callbacks)
            self.assertIn("buyamt:25", callbacks)
            self.assertIn(trading_commands.BUY_BACK_CALLBACK, callbacks)
            self.assertNotIn("buyamt:50", callbacks)
        entry = ctx.get_context(CHAT)
        self.assertEqual(entry["meta"]["state"], "buy_awaiting_usdt")
        self.assertEqual(entry["meta"]["coin"], "ARIA")
        self.assertEqual(entry["meta"]["label"], "ARIA")

    def test_typed_amount_and_quick_button_produce_same_confirmation(self):
        def _run_typed():
            ctx.set_context(CHAT, "buy", state="buy_awaiting_usdt", coin="ARIA", label="ARIA")
            with patch(f"{TC}.request_buy_confirmation") as mock_confirm, \
                 patch(f"{TC}.send_telegram_buttons"):
                self.assertTrue(ctx.try_resolve(CHAT, "25"))
                mock_confirm.assert_called_once()
                return mock_confirm.call_args.kwargs

        def _run_button():
            ctx.set_context(CHAT, "buy", state="buy_awaiting_usdt", coin="ARIA", label="ARIA")
            with patch(f"{TC}.request_buy_confirmation") as mock_confirm, \
                 patch(f"{TC}.send_telegram_buttons"), \
                 patch(f"{TC}.answer_callback_query"):
                self.assertTrue(trading_commands.handle_callback(_buyamt_query(25)))
                mock_confirm.assert_called_once()
                return mock_confirm.call_args.kwargs

        typed_kwargs = _run_typed()
        button_kwargs = _run_button()
        self.assertEqual(typed_kwargs["symbol"], button_kwargs["symbol"])
        self.assertAlmostEqual(typed_kwargs["usdt"], 25)
        self.assertAlmostEqual(button_kwargs["usdt"], 25)
        self.assertIsNone(ctx.get_context(CHAT))

    def test_invalid_amount_reply_reasks_instead_of_defaulting(self):
        ctx.set_context(CHAT, "buy", state="buy_awaiting_usdt", coin="ARIA", label="ARIA")
        with patch(f"{TC}.send_telegram_buttons") as mock_btn, \
             patch(f"{TC}.request_buy_confirmation") as mock_confirm:
            self.assertTrue(ctx.try_resolve(CHAT, "nope"))
            mock_confirm.assert_not_called()
            self.assertTrue(mock_btn.called)
        entry = ctx.get_context(CHAT)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["meta"]["state"], "buy_awaiting_usdt")
        self.assertEqual(entry["meta"]["coin"], "ARIA")

    def test_buycoin_tap_matches_typed_index(self):
        def _meta_from_typed():
            ctx.clear_context(CHAT)
            with patch(f"{TC}.prompt_buy_amount") as mock_prompt, \
                 patch(f"{TC}.request_buy_confirmation") as mock_confirm, \
                 patch(f"{TC}.send_telegram_buttons"):
                self.assertTrue(trading_commands.handle("/buy 1"))
                mock_confirm.assert_not_called()
                return mock_prompt.call_args, dict(ctx.get_context(CHAT)["meta"])

        def _meta_from_button():
            ctx.clear_context(CHAT)
            ctx.set_context(CHAT, "buy", state="buy_awaiting_coin")
            with patch(f"{TC}.prompt_buy_amount") as mock_prompt, \
                 patch(f"{TC}.request_buy_confirmation") as mock_confirm, \
                 patch(f"{TC}.send_telegram_buttons"), \
                 patch(f"{TC}.answer_callback_query"):
                self.assertTrue(trading_commands.handle_callback(_buycoin_query(1)))
                mock_confirm.assert_not_called()
                return mock_prompt.call_args, dict(ctx.get_context(CHAT)["meta"])

        typed_prompt, typed_meta = _meta_from_typed()
        button_prompt, button_meta = _meta_from_button()
        self.assertEqual(typed_prompt, button_prompt)
        self.assertEqual(typed_prompt.args[0], "ARIA")
        self.assertEqual(typed_meta["state"], "buy_awaiting_usdt")
        self.assertEqual(typed_meta["coin"], "1")
        self.assertEqual(button_meta["state"], typed_meta["state"])
        self.assertEqual(button_meta["coin"], typed_meta["coin"])

    def test_buy_back_returns_to_coin_picker(self):
        ctx.set_context(CHAT, "buy", state="buy_awaiting_usdt", coin="ARIA", label="ARIA")
        with patch(f"{TC}.request_buy_confirmation") as mock_confirm, \
             patch(f"{TC}.send_telegram_buttons") as mock_btn, \
             patch(f"{TC}.answer_callback_query"):
            self.assertTrue(trading_commands.handle_callback({
                "id": "cb-back",
                "data": trading_commands.BUY_BACK_CALLBACK,
                "message": {"chat": {"id": CHAT}},
            }))
            mock_confirm.assert_not_called()
            callbacks = [btn["callback_data"] for row in mock_btn.call_args[0][1] for btn in row]
            self.assertEqual(callbacks, ["buycoin:1", "buycoin:2"])
        entry = ctx.get_context(CHAT)
        self.assertEqual(entry["meta"]["state"], "buy_awaiting_coin")

    def test_direct_buy_with_amount_still_confirms(self):
        with patch(f"{TC}.request_buy_confirmation") as mock_confirm:
            self.assertTrue(trading_commands.handle("/buy 1 200"))
            mock_confirm.assert_called_once()
            self.assertEqual(mock_confirm.call_args.kwargs["symbol"], "ARIA/USDT")
            self.assertAlmostEqual(mock_confirm.call_args.kwargs["usdt"], 200)

    def test_legacy_build_buy_default_usdt_unchanged(self):
        self.assertEqual(
            ctx._build_command("buy", "1", {"default_usdt": "25"}),
            "/buy 1 25",
        )
        self.assertEqual(
            ctx._build_command("buy", "ARIA", {"state": "buy_awaiting_coin"}),
            "/buy ARIA",
        )

    def test_handle_callback_does_not_steal_sell_prefixes(self):
        query = {
            "id": "cb-pos",
            "data": f"{trading_commands.SELL_POS_CALLBACK_PREFIX}1",
            "message": {"chat": {"id": CHAT}},
        }
        with patch.object(trading_commands, "_handle_sell_pos_callback", return_value=True) as mock_pos, \
             patch.object(trading_commands, "_handle_buy_coin_callback", return_value=True) as mock_buy:
            self.assertTrue(trading_commands.handle_callback(query))
            mock_pos.assert_called_once_with(query)
            mock_buy.assert_not_called()


class TestAddRemoveWizards446(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        ctx._CONTEXT_FILE = Path(self.tmp.name) / "ctx.json"
        ctx.set_chat_id(CHAT)
        set_user_language("de")
        reload_messages()
        reload_menu_data()
        self.watchlist = [
            {"symbol": "ARIA/USDT", "name": "Aria", "active": True},
            {"symbol": "SOL/USDT", "name": "Solana", "active": True},
        ]

    def tearDown(self):
        ctx.clear_context(CHAT)
        ctx.set_chat_id("")
        self.tmp.cleanup()

    def test_bare_add_shows_not_on_list_taps_not_hint_only(self):
        overlay = {"coins": [{"symbol": "RAVE/USDT", "name": "Rave"}, {"symbol": "ARIA/USDT"}]}
        with patch(f"{WL}.list_coins", return_value=self.watchlist), \
             patch("data_manager.load_cmc_trending_overlay", return_value=overlay), \
             patch("data_manager.load_dry_run_overlay", return_value={"coins": []}), \
             patch(f"{WL}.send_telegram_buttons") as mock_btn, \
             patch(f"{WL}.send_telegram_message"):
            self.assertTrue(watchlist_commands.handle("/add"))
            prompt, buttons = mock_btn.call_args[0][:2]
            self.assertNotIn("/add RAVE", prompt)
            callbacks = [btn["callback_data"] for row in buttons for btn in row]
            self.assertIn("addpick:RAVE", callbacks)
            self.assertNotIn("addpick:ARIA", callbacks)
            self.assertIn(watchlist_commands.ADD_TYPE_CALLBACK, callbacks)
            self.assertIn("so nicht, schreib den Ticker", [btn["text"] for row in buttons for btn in row])
        entry = ctx.get_context(CHAT)
        self.assertEqual(entry["command"], "add")
        self.assertEqual(entry["meta"]["state"], "add_awaiting_pick")

    def test_add_type_fallback_then_typed_ticker(self):
        ctx.set_context(CHAT, "add", state="add_awaiting_pick")
        with patch(f"{WL}.send_telegram_buttons") as mock_btn, \
             patch(f"{WL}.answer_callback_query"):
            self.assertTrue(watchlist_commands.handle_callback({
                "id": "cb-type",
                "data": watchlist_commands.ADD_TYPE_CALLBACK,
                "message": {"chat": {"id": CHAT}},
            }))
            self.assertTrue(mock_btn.called)
            callbacks = [btn["callback_data"] for row in mock_btn.call_args[0][1] for btn in row]
            self.assertIn(watchlist_commands.ADD_BACK_CALLBACK, callbacks)
        self.assertEqual(ctx.get_context(CHAT)["meta"]["state"], "add_awaiting_ticker")

        with patch(f"{WL}.add_coin", return_value=(True, "RAVE/USDT wurde zur Watchlist hinzugefügt.")) as mock_add, \
             patch(f"{WL}.send_telegram_message"):
            self.assertTrue(ctx.try_resolve(CHAT, "RAVE"))
            mock_add.assert_called_once()
        self.assertIsNone(ctx.get_context(CHAT))

    def test_add_pick_tap_dispatches_add(self):
        ctx.set_context(CHAT, "add", state="add_awaiting_pick")
        with patch(f"{WL}.add_coin", return_value=(True, "ok")) as mock_add, \
             patch(f"{WL}.send_telegram_message"), \
             patch(f"{WL}.answer_callback_query"):
            self.assertTrue(watchlist_commands.handle_callback({
                "id": "cb-pick",
                "data": f"{watchlist_commands.ADD_PICK_CALLBACK_PREFIX}RAVE",
                "message": {"chat": {"id": CHAT}},
            }))
            mock_add.assert_called_once()
        self.assertIsNone(ctx.get_context(CHAT))

    def test_bare_remove_shows_tappable_on_list_coins(self):
        with patch(f"{WL}.list_coins", return_value=self.watchlist), \
             patch(f"{WL}.send_telegram_buttons") as mock_btn, \
             patch(f"{WL}.send_telegram_message"):
            self.assertTrue(watchlist_commands.handle("/remove"))
            prompt, buttons = mock_btn.call_args[0][:2]
            self.assertNotIn("/remove 2", prompt)
            self.assertEqual(
                [row[0]["callback_data"] for row in buttons],
                ["rempick:1", "rempick:2"],
            )
            self.assertIn("ARIA", buttons[0][0]["text"])
            self.assertIn("SOL", buttons[1][0]["text"])
        entry = ctx.get_context(CHAT)
        self.assertEqual(entry["command"], "remove")
        self.assertEqual(entry["meta"]["state"], "remove_awaiting_pick")

    def test_remove_tap_matches_typed_index(self):
        ctx.set_context(CHAT, "remove", state="remove_awaiting_pick")
        with patch(f"{WL}.list_coins", return_value=self.watchlist), \
             patch(f"{WL}.remove_coin", return_value=(True, "ok")) as mock_rm, \
             patch(f"{WL}.send_telegram_message"), \
             patch(f"{WL}.answer_callback_query"):
            self.assertTrue(watchlist_commands.handle_callback({
                "id": "cb-rem",
                "data": f"{watchlist_commands.REM_PICK_CALLBACK_PREFIX}2",
                "message": {"chat": {"id": CHAT}},
            }))
            mock_rm.assert_called_once()
            self.assertEqual(mock_rm.call_args.args[0], "SOL/USDT")
        self.assertIsNone(ctx.get_context(CHAT))

    def test_add_remove_callbacks_missing_chat_do_not_mutate(self):
        ctx.set_context(CHAT, "add", state="add_awaiting_pick")
        with patch(f"{WL}.add_coin") as mock_add, \
             patch(f"{WL}.answer_callback_query"), \
             patch("logger.log"):
            self.assertTrue(watchlist_commands.handle_callback({
                "id": "cb",
                "data": f"{watchlist_commands.ADD_PICK_CALLBACK_PREFIX}RAVE",
            }))
            mock_add.assert_not_called()
        self.assertEqual(ctx.get_context(CHAT)["command"], "add")


if __name__ == "__main__":
    unittest.main()
