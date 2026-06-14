import unittest
from unittest.mock import patch

from notifications.telegram_commands.menu_commands import (
    MENU_SECTIONS,
    _home_keyboard,
    _section_keyboard,
    handle,
    handle_callback,
)


class TestMenuCommands(unittest.TestCase):
    def test_sections_cover_key_commands(self):
        all_keys = {k for _, _, keys in MENU_SECTIONS for k in keys}
        self.assertIn("buy", all_keys)
        self.assertIn("hermes_run", all_keys)
        self.assertGreaterEqual(len(all_keys), 25)

    def test_home_keyboard_has_six_sections(self):
        rows = _home_keyboard()
        buttons = [b for row in rows for b in row]
        self.assertEqual(len(buttons), 6)

    def test_section_keyboard_has_back(self):
        rows = _section_keyboard("handel")
        flat = [b["callback_data"] for row in rows for b in row]
        self.assertIn("menu:home", flat)
        self.assertTrue(any(c.startswith("menu:run:") for c in flat))

    def test_handle_menu_command(self):
        with patch("notifications.telegram_commands.menu_commands.show_home", return_value=True) as mock_show:
            self.assertTrue(handle("/menu"))
            mock_show.assert_called_once()

    def test_callback_home_edits_message(self):
        cb = {
            "id": "cq1",
            "data": "menu:home",
            "message": {"chat": {"id": 123}, "message_id": 45},
        }
        with patch("notifications.telegram_commands.menu_commands.answer_callback_query"), \
             patch("notifications.telegram_commands.menu_commands.show_home", return_value=True) as mock_show:
            self.assertTrue(handle_callback(cb))
            mock_show.assert_called_once_with(chat_id=123, message_id=45)

    def test_callback_run_dispatches_command(self):
        cb = {"id": "cq2", "data": "menu:run:positions", "message": {"chat": {"id": 1}, "message_id": 2}}
        with patch("notifications.telegram_commands.menu_commands.answer_callback_query"), \
             patch("notifications.telegram_commands.router.dispatch_command", return_value=True) as mock_dispatch:
            self.assertTrue(handle_callback(cb))
            mock_dispatch.assert_called_once_with("/positions")


if __name__ == "__main__":
    unittest.main()