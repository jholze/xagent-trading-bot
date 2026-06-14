import unittest
from unittest.mock import MagicMock, patch

from notifications.telegram_commands.command_menu import (
    TELEGRAM_MENU_COMMAND_KEYS,
    all_bot_commands,
    register_bot_commands,
)
from notifications.telegram_commands.usage_hints import USAGE


class TestCommandMenu(unittest.TestCase):
    def test_menu_has_35_commands(self):
        self.assertEqual(len(TELEGRAM_MENU_COMMAND_KEYS), 35)
        self.assertEqual(len(set(TELEGRAM_MENU_COMMAND_KEYS)), 35)

    def test_all_commands_have_menu_description(self):
        for key in TELEGRAM_MENU_COMMAND_KEYS:
            self.assertIn("menu_description", USAGE[key])
            self.assertTrue(USAGE[key]["menu_description"].strip())

    def test_all_bot_commands_format(self):
        commands = all_bot_commands()
        self.assertEqual(len(commands), 35)
        for entry in commands:
            self.assertIn("command", entry)
            self.assertIn("description", entry)
            self.assertRegex(entry["command"], r"^[a-z0-9_]{1,32}$")
            self.assertLessEqual(len(entry["description"]), 256)

    def test_register_bot_commands_calls_telegram_api(self):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.content = b'{"ok": true}'
        mock_resp.json.return_value = {"ok": True}

        with patch("notifications.telegram_commands.command_menu.requests.post", return_value=mock_resp) as mock_post:
            ok = register_bot_commands(token="test-token")

        self.assertTrue(ok)
        self.assertEqual(mock_post.call_count, 2)
        first_url = mock_post.call_args_list[0][0][0]
        second_url = mock_post.call_args_list[1][0][0]
        self.assertIn("/setMyCommands", first_url)
        self.assertIn("/setChatMenuButton", second_url)

        payload = mock_post.call_args_list[0][1]["json"]
        self.assertEqual(len(payload["commands"]), 35)
        self.assertEqual(payload["language_code"], "de")
        self.assertEqual(
            mock_post.call_args_list[1][1]["json"],
            {"menu_button": {"type": "commands"}},
        )

    def test_register_without_token_returns_false(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(register_bot_commands(token=None))


if __name__ == "__main__":
    unittest.main()