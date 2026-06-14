import unittest
from unittest.mock import MagicMock, patch

from notifications.telegram_commands.command_menu import (
    TELEGRAM_MENU_COMMAND_KEYS,
    all_bot_commands,
    menu_button_payload,
    register_bot_commands,
)
from notifications.telegram_commands.menu_commands import MENU_SECTIONS
from notifications.telegram_commands.usage_hints import USAGE


class TestCommandMenu(unittest.TestCase):
    def test_menu_has_all_section_commands(self):
        expected = [k for _, _, _, keys in MENU_SECTIONS for k in keys]
        self.assertEqual(TELEGRAM_MENU_COMMAND_KEYS, expected)
        self.assertEqual(len(TELEGRAM_MENU_COMMAND_KEYS), 36)
        self.assertEqual(len(set(TELEGRAM_MENU_COMMAND_KEYS)), 36)

    def test_all_commands_have_menu_description(self):
        for key in TELEGRAM_MENU_COMMAND_KEYS:
            self.assertIn("menu_description", USAGE[key])
            self.assertTrue(USAGE[key]["menu_description"].strip())

    def test_all_bot_commands_have_section_prefix(self):
        commands = all_bot_commands()
        self.assertEqual(len(commands), 36)
        for entry in commands:
            self.assertIn("command", entry)
            self.assertIn("description", entry)
            self.assertRegex(entry["command"], r"^[a-z0-9_]{1,32}$")
            self.assertIn("·", entry["description"])
            self.assertLessEqual(len(entry["description"]), 256)

    def test_menu_button_has_title(self):
        payload = menu_button_payload()
        self.assertEqual(payload["type"], "commands")
        self.assertTrue(payload["text"].strip())
        self.assertLessEqual(len(payload["text"]), 64)

    def test_register_bot_commands_calls_telegram_api(self):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.content = b'{"ok": true}'
        mock_resp.json.return_value = {"ok": True}

        with patch("notifications.telegram_commands.command_menu.requests.post", return_value=mock_resp) as mock_post, \
             patch("notifications.telegram_commands.command_menu.menu_button_payload", return_value={"type": "commands", "text": "Menü"}), \
             patch("notifications.telegram_commands.command_menu.send_main_section_keyboard", return_value=True):
            ok = register_bot_commands(token="test-token")

        self.assertTrue(ok)
        self.assertEqual(mock_post.call_count, 2)
        payload = mock_post.call_args_list[0][1]["json"]
        self.assertEqual(len(payload["commands"]), 36)
        self.assertEqual(payload["language_code"], "de")

    def test_register_without_token_returns_false(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(register_bot_commands(token=None))


if __name__ == "__main__":
    unittest.main()