"""#454 — pending command_context must not stay armed after success or swallow the keyboard."""

from __future__ import annotations

import ast
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from notifications.telegram_commands import command_context as ctx
from notifications.telegram_commands import lock_commands, short_commands, trading_commands
from notifications.telegram_commands.menu_i18n import back_label, help_label, section_title
from telegram_notifier import handle_telegram_text

LC = "notifications.telegram_commands.lock_commands"
SC = "notifications.telegram_commands.short_commands"
TC = "notifications.telegram_commands.trading_commands"

CHAT = "454"

_BUILD_SAMPLES = {
    "buy": ("1", {"default_usdt": "25"}, "/buy 1 25"),
    "sell": ("RAVE", {}, "/sell RAVE"),
    "add": ("RAVE", {}, "/add RAVE"),
    "remove": ("1", {}, "/remove 1"),
    "why": ("RAVE", {}, "/why RAVE"),
    "ask": ("hello", {}, "/ask hello"),
    "orders": ("1", {}, "/orders 1"),
    "maxpositions": ("3", {}, "/maxpositions 3"),
    "mode": ("paper", {}, "/mode paper"),
    "addx": ("user", {}, "/addx user"),
    "removex": ("user", {}, "/removex user"),
    "sandbox_results": ("abc", {}, "/sandbox_results abc"),
    "sandbox_promote": ("abc", {}, "/sandbox_promote abc"),
    "backtest_lock": ("BLESS", {}, "/backtest_lock BLESS/USDT"),
    "backtest_results": ("BLESS", {}, "/backtest_results BLESS/USDT"),
    "testaccount": ("user", {}, "/testaccount user"),
    "lock": ("BLESS", {}, "/lock BLESS"),
    "unlock": ("BLESS", {}, "/unlock BLESS"),
    "short": ("H", {}, "/short H"),
    "cover": ("H 50", {}, "/cover H 50"),
}


def _commands_named_in_build() -> set[str]:
    tree = ast.parse(inspect.getsource(ctx._build_command))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        left = node.left
        if not (isinstance(left, ast.Name) and left.id == "command"):
            continue
        for op, comparator in zip(node.ops, node.comparators):
            if isinstance(op, ast.Eq) and isinstance(comparator, ast.Constant):
                names.add(comparator.value)
            elif isinstance(op, ast.In):
                if isinstance(comparator, (ast.Tuple, ast.List, ast.Set)):
                    for elt in comparator.elts:
                        if isinstance(elt, ast.Constant):
                            names.add(elt.value)
    return names


class TestCommandContextUnarmed454(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ctx.json"
        ctx._CONTEXT_FILE = self.path
        ctx.set_chat_id(CHAT)

    def tearDown(self):
        ctx.set_chat_id("")
        self.tmp.cleanup()

    def test_every_resolvable_command_has_build_branch(self):
        self.assertEqual(set(_BUILD_SAMPLES), ctx._RESOLVABLE_COMMANDS)
        self.assertEqual(_commands_named_in_build(), ctx._RESOLVABLE_COMMANDS)
        for cmd, (text, meta, expected) in _BUILD_SAMPLES.items():
            self.assertEqual(ctx._build_command(cmd, text, meta), expected, cmd)

    def test_unknown_command_does_not_arm(self):
        ctx.activate_command("orders_blocked")
        ctx.activate_command("orders_month")
        self.assertIsNone(ctx.get_context(CHAT))

    def test_lock_with_symbol_does_not_arm(self):
        pos = {"symbol": "BLESS/USDT", "timeframe": "1h", "amount": 10}
        lock = {"until": None, "modes": ["auto_sell"], "reason": "telegram_lock"}
        with patch(f"{LC}.position_locks_enabled", return_value=True), \
             patch(f"{LC}.list_active_positions", return_value=[pos]), \
             patch(f"{LC}.get_prices_batch", return_value={"BLESS/USDT": 1.0}), \
             patch(f"{LC}.resolve_position_by_symbol", return_value=pos), \
             patch(f"{LC}.get_position", return_value=pos), \
             patch(f"{LC}.is_open_position", return_value=True), \
             patch(f"{LC}.parse_duration_to_until", return_value=None), \
             patch(f"{LC}.build_lock", return_value=lock), \
             patch(f"{LC}.set_position_lock") as set_lock, \
             patch(f"{LC}.send_telegram_message"), \
             patch(f"{LC}.send_telegram_buttons"):
            self.assertTrue(lock_commands.handle("/lock BLESS"))
            # #453: /lock SYMBOL only prompts; persist happens on lock_ok.
            set_lock.assert_not_called()
        self.assertIsNone(ctx.get_context(CHAT))
        self.assertFalse(ctx.try_resolve(CHAT, section_title("handel", "de")))

    def test_lock_without_symbol_arms_and_builds(self):
        pos = {"symbol": "BLESS/USDT", "timeframe": "1h", "amount": 10}
        with patch(f"{LC}.position_locks_enabled", return_value=True), \
             patch(f"{LC}.list_active_positions", return_value=[pos]), \
             patch(f"{LC}.get_position", return_value=pos), \
             patch(f"{LC}.get_lock", return_value=None), \
             patch(f"{LC}.send_telegram_message"):
            self.assertTrue(lock_commands.handle("/lock"))
        entry = ctx.get_context(CHAT)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["command"], "lock")
        with patch("notifications.telegram_commands.router.dispatch_command", return_value=True) as mock:
            self.assertTrue(ctx.try_resolve(CHAT, "BLESS"))
            mock.assert_called_once_with("/lock BLESS")

    def test_unlock_short_cover_with_args_do_not_arm(self):
        pos = {"symbol": "H/USDT", "timeframe": "4h", "amount": 10, "side": "short"}
        lock = {"until": None, "modes": ["auto_sell"]}
        with patch(f"{LC}.list_active_positions", return_value=[pos]), \
             patch(f"{LC}.get_prices_batch", return_value={"H/USDT": 1.0}), \
             patch(f"{LC}.resolve_position_by_symbol", return_value=pos), \
             patch(f"{LC}.get_position", return_value=pos), \
             patch(f"{LC}.get_lock", return_value=lock), \
             patch(f"{LC}.set_position_lock"), \
             patch(f"{LC}.send_telegram_message"):
            self.assertTrue(lock_commands.handle("/unlock H"))
        self.assertIsNone(ctx.get_context(CHAT))

        cfg = MagicMock()
        cfg.raw = {"shorts": {"enabled": True}}
        with patch(f"{SC}.get_bot_config", return_value=cfg), \
             patch(f"{SC}.shorts_enabled", return_value=True), \
             patch(f"{SC}.get_prices_batch", return_value={"H/USDT": 0.05}), \
             patch(f"{SC}.request_short_confirmation", return_value=True):
            self.assertTrue(short_commands.handle("/short H"))
        self.assertIsNone(ctx.get_context(CHAT))

        with patch(f"{SC}.list_active_positions", return_value=[pos]), \
             patch(f"{SC}.get_prices_batch", return_value={"H/USDT": 0.04}), \
             patch(f"{SC}.get_position", return_value=pos), \
             patch(f"{SC}.request_cover_confirmation", return_value=True):
            self.assertTrue(short_commands.handle("/cover H"))
        self.assertIsNone(ctx.get_context(CHAT))

    def test_buy_and_sell_without_args_still_arm(self):
        coins = [{"symbol": "ARIA/USDT", "name": "Aria", "active": True}]
        with patch(f"{TC}.list_coins", return_value=coins), \
             patch(f"{TC}.get_prices_batch", return_value={"ARIA/USDT": 0.05}), \
             patch(f"{TC}.send_telegram_message"):
            self.assertTrue(trading_commands.handle("/buy"))
        entry = ctx.get_context(CHAT)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["command"], "buy")

        ctx.clear_context(CHAT)
        lots = [{"symbol": "RAVE/USDT", "timeframe": "1h", "amount": 100.0, "average_entry": 0.5}]
        with patch(f"{TC}.list_active_positions", return_value=lots), \
             patch(f"{TC}.get_prices_batch", return_value={"RAVE/USDT": 0.65}), \
             patch(f"{TC}.send_telegram_message"), \
             patch(f"{TC}.send_telegram_buttons"):
            self.assertTrue(trading_commands.handle("/sell"))
        entry = ctx.get_context(CHAT)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["command"], "sell")

    def test_try_resolve_clears_on_back_and_section_title(self):
        ctx.set_context(CHAT, "ask")
        back = back_label("de")
        with patch("notifications.telegram_commands.router.dispatch_command") as dispatch, \
             patch("notifications.telegram_commands.command_context.send_telegram_message") as send:
            self.assertFalse(ctx.try_resolve(CHAT, back))
            dispatch.assert_not_called()
            send.assert_not_called()
        self.assertIsNone(ctx.get_context(CHAT))

        ctx.set_context(CHAT, "buy", default_usdt=200)
        title = section_title("handel", "de")
        with patch("notifications.telegram_commands.router.dispatch_command") as dispatch:
            self.assertFalse(ctx.try_resolve(CHAT, title))
            dispatch.assert_not_called()
        self.assertIsNone(ctx.get_context(CHAT))

        ctx.set_context(CHAT, "ask")
        with patch("notifications.telegram_commands.router.dispatch_command") as dispatch:
            self.assertFalse(ctx.try_resolve(CHAT, "◀ Bereiche"))
            dispatch.assert_not_called()
        self.assertIsNone(ctx.get_context(CHAT))

        ctx.set_context(CHAT, "ask")
        with patch("notifications.telegram_commands.router.dispatch_command") as dispatch:
            self.assertFalse(ctx.try_resolve(CHAT, help_label("de")))
            dispatch.assert_not_called()
        self.assertIsNone(ctx.get_context(CHAT))

    def test_handle_telegram_text_keyboard_path_runs_after_clear(self):
        ctx.set_context(CHAT, "ask")
        title = section_title("handel", "de")
        with patch("notifications.telegram_commands.onboarding_commands.handle", return_value=False), \
             patch(
                 "notifications.telegram_commands.menu_commands.handle_text",
                 return_value=True,
             ) as keyboard, \
             patch("notifications.telegram_commands.router.dispatch_command") as dispatch:
            self.assertTrue(handle_telegram_text(title, chat_id=CHAT))
            dispatch.assert_not_called()
            keyboard.assert_called_once_with(title, chat_id=CHAT)
        self.assertIsNone(ctx.get_context(CHAT))

        ctx.set_context(CHAT, "buy", default_usdt=200)
        back = back_label("de")
        with patch("notifications.telegram_commands.onboarding_commands.handle", return_value=False), \
             patch(
                 "notifications.telegram_commands.menu_commands.handle_text",
                 return_value=True,
             ) as keyboard, \
             patch("notifications.telegram_commands.router.dispatch_command") as dispatch:
            self.assertTrue(handle_telegram_text(back, chat_id=CHAT))
            dispatch.assert_not_called()
            keyboard.assert_called_once_with(back, chat_id=CHAT)
        self.assertIsNone(ctx.get_context(CHAT))


if __name__ == "__main__":
    unittest.main()
