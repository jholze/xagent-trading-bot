"""#450 — pending command context shows Cancel; tap clears context and places no order.

Nothing here writes into ``data/``: command context lives in a tmp file.
Existing sell-wizard assertions in ``test_sell_guided_flow.py`` stay untouched.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from notifications.telegram_commands import command_context as ctx
from notifications.telegram_commands import trading_commands, watchlist_commands
from notifications.telegram_commands.menu_i18n import set_user_language
from notifications.telegram_commands.router import (
    _is_operator_only_callback,
    dispatch_callback,
)
from notifications.telegram_i18n import reload_messages, t

TC = "notifications.telegram_commands.trading_commands"
CHAT = "450"


def _cancel_query(chat_id: str = CHAT, *, missing_chat: bool = False) -> dict:
    q = {
        "id": "cb-cancel-450",
        "data": ctx.CANCEL_CALLBACK,
    }
    if missing_chat:
        return q
    q["message"] = {"chat": {"id": chat_id}}
    return q


def _markup_callbacks(markup) -> list[str]:
    keyboard = (markup or {}).get("inline_keyboard") or []
    return [btn.get("callback_data") for row in keyboard for btn in row]


def _has_cancel(markup) -> bool:
    return ctx.CANCEL_CALLBACK in _markup_callbacks(markup)


class TestTelegramCancelPending450(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        ctx._CONTEXT_FILE = Path(self.tmp.name) / "ctx.json"
        ctx.set_chat_id(CHAT)
        set_user_language("de")
        reload_messages()

    def tearDown(self):
        ctx.clear_context(CHAT)
        ctx.set_chat_id("")
        self.tmp.cleanup()

    def test_ttl_stays_fifteen_minutes(self):
        self.assertEqual(ctx._TTL_MINUTES, 15)

    def test_cancel_keyboard_is_abbrechen_and_does_not_encode_an_order(self):
        rows = ctx.cancel_keyboard()
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]), 1)
        btn = rows[0][0]
        self.assertEqual(btn["callback_data"], ctx.CANCEL_CALLBACK)
        self.assertIn("Abbrechen", btn["text"])
        self.assertNotIn("sellpct:", btn["callback_data"])
        self.assertNotIn("sellpos:", btn["callback_data"])
        self.assertNotIn("manual_ok", btn["callback_data"])

    def test_buy_prompt_includes_cancel_and_does_not_place_an_order(self):
        coins = [{"symbol": "ARIA/USDT", "name": "Aria", "active": True}]
        cfg = MagicMock()
        cfg.max_usdt_per_trade = 25
        with patch(f"{TC}.list_coins", return_value=coins), \
             patch(f"{TC}.get_prices_batch", return_value={"ARIA/USDT": 0.05}), \
             patch(f"{TC}.get_bot_config", return_value=cfg), \
             patch(f"{TC}.request_buy_confirmation") as mock_confirm, \
             patch(f"{TC}.send_telegram_message") as mock_send:
            self.assertTrue(trading_commands.handle("/buy"))
            mock_confirm.assert_not_called()
            self.assertTrue(mock_send.called)
            text, kwargs_markup = mock_send.call_args[0][0], mock_send.call_args.kwargs.get(
                "reply_markup"
            )
            if kwargs_markup is None and len(mock_send.call_args[0]) > 1:
                kwargs_markup = mock_send.call_args[0][1]
            self.assertIn("Coins kaufen", text)
            self.assertTrue(_has_cancel(kwargs_markup))
        entry = ctx.get_context(CHAT)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["command"], "buy")
        self.assertNotIn("chrome", entry.get("meta") or {})

    def test_sell_position_chrome_has_cancel_and_does_not_place_an_order(self):
        lots = [{"symbol": "RAVE/USDT", "timeframe": "1h", "amount": 100.0, "average_entry": 0.5}]
        with patch(f"{TC}.list_active_positions", return_value=lots), \
             patch(f"{TC}.get_prices_batch", return_value={"RAVE/USDT": 0.65}), \
             patch(f"{TC}.request_sell_confirmation") as mock_confirm, \
             patch(f"{TC}.send_telegram_buttons"), \
             patch(f"{TC}.send_telegram_message"), \
             patch("notifications.telegram_commands.command_context.send_telegram_message") as chrome:
            self.assertTrue(trading_commands.handle("/sell"))
            mock_confirm.assert_not_called()
            self.assertTrue(chrome.called)
            reminder = chrome.call_args[0][0]
            markup = chrome.call_args.kwargs.get("reply_markup")
            if markup is None and len(chrome.call_args[0]) > 1:
                markup = chrome.call_args[0][1]
            self.assertIn("Verkauf", reminder)
            self.assertIn("Abbrechen", reminder)
            self.assertTrue(_has_cancel(markup))
        entry = ctx.get_context(CHAT)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["command"], "sell")
        self.assertEqual(entry["meta"]["state"], "sell_awaiting_position")

    def test_sell_pct_chrome_has_cancel_and_does_not_place_an_order(self):
        lots = [{"symbol": "RAVE/USDT", "timeframe": "1h", "amount": 200.0, "average_entry": 0.5}]
        with patch(f"{TC}.list_active_positions", return_value=lots), \
             patch(f"{TC}.get_prices_batch", return_value={"RAVE/USDT": 0.65}), \
             patch(f"{TC}.get_prices", return_value=(0.65, 0.65, None)), \
             patch(f"{TC}.get_position", return_value={"amount": 200.0}), \
             patch(f"{TC}.request_sell_confirmation") as mock_confirm, \
             patch(f"{TC}.send_telegram_buttons") as mock_btn, \
             patch("notifications.telegram_commands.command_context.send_telegram_message") as chrome:
            self.assertTrue(trading_commands.handle("/sell RAVE"))
            mock_confirm.assert_not_called()
            callback_data = [btn["callback_data"] for row in mock_btn.call_args[0][1] for btn in row]
            # Frozen sell-wizard percent row is unchanged; Cancel is chrome, not a fifth pct button.
            self.assertEqual(callback_data, ["sellpct:25", "sellpct:50", "sellpct:75", "sellpct:100"])
            self.assertTrue(chrome.called)
            markup = chrome.call_args.kwargs.get("reply_markup")
            if markup is None and len(chrome.call_args[0]) > 1:
                markup = chrome.call_args[0][1]
            self.assertTrue(_has_cancel(markup))
            self.assertIn("RAVE", chrome.call_args[0][0])
        entry = ctx.get_context(CHAT)
        self.assertEqual(entry["meta"]["state"], "sell_awaiting_pct")
        self.assertEqual(entry["meta"]["position"], "RAVE")

    def test_cancel_buy_clears_context_and_does_not_dispatch_an_order(self):
        ctx.activate_command("buy", default_usdt=25, chrome=False)
        self.assertIsNotNone(ctx.get_context(CHAT))
        with patch("telegram_notifier.answer_callback_query") as ack, \
             patch("notifications.telegram_commands.command_context.send_telegram_message") as send, \
             patch("notifications.telegram_commands.router.dispatch_command") as dispatch, \
             patch(f"{TC}.request_buy_confirmation") as mock_confirm:
            self.assertTrue(ctx.handle_callback(_cancel_query()))
            ack.assert_called_once()
            dispatch.assert_not_called()
            mock_confirm.assert_not_called()
            self.assertIn("nichts ausgeführt", send.call_args[0][0])
        self.assertIsNone(ctx.get_context(CHAT))
        with patch("notifications.telegram_commands.router.dispatch_command") as dispatch:
            self.assertFalse(ctx.try_resolve(CHAT, "1 25"))
            dispatch.assert_not_called()

    def test_cancel_sell_clears_context_and_does_not_place_an_order(self):
        ctx.activate_command("sell", chrome=False, state="sell_awaiting_pct", position="RAVE", label="RAVE")
        self.assertIsNotNone(ctx.get_context(CHAT))
        with patch("telegram_notifier.answer_callback_query"), \
             patch("notifications.telegram_commands.command_context.send_telegram_message"), \
             patch("notifications.telegram_commands.router.dispatch_command") as dispatch, \
             patch(f"{TC}.request_sell_confirmation") as mock_confirm:
            self.assertTrue(dispatch_callback(_cancel_query()))
            dispatch.assert_not_called()
            mock_confirm.assert_not_called()
        self.assertIsNone(ctx.get_context(CHAT))
        with patch("notifications.telegram_commands.router.dispatch_command") as dispatch:
            self.assertFalse(ctx.try_resolve(CHAT, "50"))
            dispatch.assert_not_called()

    def test_cancel_callback_without_armed_context_still_does_not_order(self):
        self.assertIsNone(ctx.get_context(CHAT))
        with patch("telegram_notifier.answer_callback_query"), \
             patch("notifications.telegram_commands.command_context.send_telegram_message"), \
             patch("notifications.telegram_commands.router.dispatch_command") as dispatch:
            self.assertTrue(ctx.handle_callback(_cancel_query()))
            dispatch.assert_not_called()
        self.assertIsNone(ctx.get_context(CHAT))

    def test_cancel_callback_missing_chat_id_does_not_pop_operator_context(self):
        """Fail closed like #498 lock_ok: no chat.id must not pop TELEGRAM_CHAT_ID."""
        operator = "100"
        ctx.set_chat_id("")
        ctx.set_context(operator, "buy", default_usdt=25)
        query = _cancel_query(missing_chat=True)
        with patch.dict(os.environ, {"TELEGRAM_CHAT_ID": operator}, clear=False), \
             patch("telegram_notifier.answer_callback_query") as ack, \
             patch("notifications.telegram_commands.command_context.send_telegram_message") as send, \
             patch("logger.log") as mock_log, \
             patch("notifications.telegram_commands.router.dispatch_command") as dispatch:
            self.assertTrue(ctx.handle_callback(query))
            ack.assert_called_once_with("cb-cancel-450")
            send.assert_not_called()
            dispatch.assert_not_called()
            mock_log.assert_any_call("cmdctx cancel callback missing chat id", "WARNING")
        entry = ctx.get_context(operator)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["command"], "buy")
        with patch("notifications.telegram_commands.router.dispatch_command", return_value=True) as mock:
            self.assertTrue(ctx.try_resolve(operator, "1"))
            mock.assert_called_once_with("/buy 1 25")

    def test_cancel_callback_is_not_operator_only(self):
        self.assertFalse(_is_operator_only_callback(ctx.CANCEL_CALLBACK))

    def test_satellite_cancel_callback_clears_satellite_context(self):
        satellite = "999"
        ctx.set_chat_id(satellite)
        ctx.set_context(satellite, "buy", default_usdt=25)
        with patch("core.tenant_context.multi_tenant_enabled", return_value=True), \
             patch(
                 "storage.tenant_registry.find_tenant_by_owner_chat_id",
                 return_value={"tenant_id": "henry"},
             ), \
             patch("telegram_notifier.answer_callback_query"), \
             patch("notifications.telegram_commands.command_context.send_telegram_message"), \
             patch("notifications.telegram_commands.router.dispatch_command") as dispatch:
            self.assertTrue(dispatch_callback(_cancel_query(satellite)))
            dispatch.assert_not_called()
        self.assertIsNone(ctx.get_context(satellite))

    def test_operator_only_callback_denied_before_cancel_handler(self):
        cb = {
            "id": "cq",
            "data": "config_ok:tok",
            "message": {"chat": {"id": 999}},
        }
        with patch("core.tenant_context.multi_tenant_enabled", return_value=True), \
             patch(
                 "storage.tenant_registry.find_tenant_by_owner_chat_id",
                 return_value={"tenant_id": "henry"},
             ), \
             patch(
                 "notifications.telegram_commands.command_context.handle_callback"
             ) as cancel_cb, \
             patch("notifications.telegram_commands.router.answer_callback_query") as ack, \
             patch("notifications.telegram_commands.config_commands.handle_callback") as cfg:
            self.assertTrue(dispatch_callback(cb))
            cancel_cb.assert_not_called()
            cfg.assert_not_called()
            ack.assert_called_once()
            self.assertIn("Nur Operator", ack.call_args.args[1])

    def test_add_and_ask_waiting_prompts_get_cancel_chrome(self):
        with patch("notifications.telegram_commands.watchlist_commands.send_telegram_message"), \
             patch("notifications.telegram_commands.command_context.send_telegram_message") as chrome:
            self.assertTrue(watchlist_commands.handle("/add"))
            self.assertTrue(chrome.called)
            markup = chrome.call_args.kwargs.get("reply_markup")
            if markup is None and len(chrome.call_args[0]) > 1:
                markup = chrome.call_args[0][1]
            self.assertTrue(_has_cancel(markup))
            self.assertIn("/add", chrome.call_args[0][0])
        entry = ctx.get_context(CHAT)
        self.assertEqual(entry["command"], "add")

        ctx.clear_context(CHAT)
        with patch("notifications.telegram_commands.ask_commands.send_telegram_message"), \
             patch("notifications.telegram_commands.command_context.send_telegram_message") as chrome:
            from notifications.telegram_commands import ask_commands

            self.assertTrue(ask_commands.handle("/ask"))
            markup = chrome.call_args.kwargs.get("reply_markup")
            if markup is None and len(chrome.call_args[0]) > 1:
                markup = chrome.call_args[0][1]
            self.assertTrue(_has_cancel(markup))
        self.assertEqual(ctx.get_context(CHAT)["command"], "ask")

    def test_unrelated_callback_is_not_claimed(self):
        ctx.activate_command("buy", default_usdt=25, chrome=False)
        self.assertFalse(ctx.handle_callback({
            "id": "cb",
            "data": "sellpct:50",
            "message": {"chat": {"id": CHAT}},
        }))
        self.assertIsNotNone(ctx.get_context(CHAT))

    def test_pending_reminder_strings_exist(self):
        self.assertIn("Kauf", t("pending_reminder_buy"))
        self.assertIn("Verkauf", t("pending_reminder_sell"))
        self.assertIn("RAVE", t("pending_reminder_sell_pct", position="RAVE"))
        self.assertEqual(t("pending_cancel_done").count("nichts ausgeführt"), 1)


if __name__ == "__main__":
    unittest.main()
