import unittest
from unittest.mock import patch

from notifications.telegram_commands.menu_commands import (
    MENU_SECTIONS,
    _home_keyboard,
    _section_reply_rows,
    all_menu_command_keys,
    handle,
    handle_callback,
    handle_text,
)
from notifications.telegram_commands.menu_i18n import back_label, help_label, set_user_language


class TestMenuCommands(unittest.TestCase):
    def test_all_commands_in_sections(self):
        keys = all_menu_command_keys()
        self.assertEqual(len(keys), 40)
        self.assertIn("positions_full", keys)
        self.assertIn("lc", keys)
        self.assertIn("sandbox_results", keys)
        self.assertIn("backtest_lock", keys)

    def test_seven_sections(self):
        self.assertEqual(len(MENU_SECTIONS), 7)

    def test_home_keyboard_has_sections(self):
        buttons = [b for row in _home_keyboard() for b in row]
        self.assertEqual(len(buttons), 7)

    def test_section_reply_rows_include_commands_and_back(self):
        rows = _section_reply_rows("handel")
        flat = [cell for row in rows for cell in row]
        self.assertIn("/positions full", flat)
        self.assertIn("/buy", flat)
        self.assertIn(back_label("de"), flat)
        self.assertIn(help_label("de"), flat)

    def test_handle_menu_command(self):
        with patch("notifications.telegram_commands.menu_commands.send_main_section_keyboard", return_value=True), \
             patch("notifications.telegram_commands.menu_commands.send_telegram_buttons", return_value=True):
            self.assertTrue(handle("/menu"))

    def test_handle_text_section_opens_subkeyboard(self):
        set_user_language("de")
        from notifications.telegram_commands.menu_i18n import section_title

        title = section_title("watchlist", "de")
        with patch("notifications.telegram_commands.menu_commands.send_section_keyboard", return_value=True) as mock_sec:
            self.assertTrue(handle_text(title))
            mock_sec.assert_called_once_with("watchlist", chat_id=None)

    def test_handle_text_back_returns_main(self):
        with patch("notifications.telegram_commands.menu_commands.send_main_section_keyboard", return_value=True) as mock_main:
            self.assertTrue(handle_text(back_label("de")))
            mock_main.assert_called_once()

    def test_callback_run_dispatches_command(self):
        cb = {"id": "cq2", "data": "menu:run:positions", "message": {"chat": {"id": 1}, "message_id": 2}}
        with patch("notifications.telegram_commands.menu_commands.answer_callback_query"), \
             patch("notifications.telegram_commands.router.dispatch_command", return_value=True) as mock_dispatch:
            self.assertTrue(handle_callback(cb))
            mock_dispatch.assert_called_once_with("/positions")

    def test_callback_run_dispatches_positions_full(self):
        cb = {"id": "cq3", "data": "menu:run:positions_full", "message": {"chat": {"id": 1}, "message_id": 2}}
        with patch("notifications.telegram_commands.menu_commands.answer_callback_query"), \
             patch("notifications.telegram_commands.router.dispatch_command", return_value=True) as mock_dispatch:
            self.assertTrue(handle_callback(cb))
            mock_dispatch.assert_called_once_with("/positions full")


if __name__ == "__main__":
    unittest.main()