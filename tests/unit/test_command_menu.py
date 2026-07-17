import unittest
from unittest.mock import MagicMock, patch

from notifications.telegram_commands.command_menu import (
    TELEGRAM_MENU_COMMAND_KEYS,
    all_bot_commands,
    menu_button_payload,
    register_bot_commands,
    register_commands_for_chat,
)
from notifications.telegram_commands.menu_commands import MENU_SECTIONS, MENU_SECTIONS_SATELLITE
from notifications.telegram_commands.usage_hints import USAGE, _ensure_usage_cache


class TestCommandMenu(unittest.TestCase):
    def test_menu_has_all_section_commands(self):
        expected = [k for _, keys in MENU_SECTIONS for k in keys]
        self.assertEqual(TELEGRAM_MENU_COMMAND_KEYS, expected)
        self.assertEqual(len(TELEGRAM_MENU_COMMAND_KEYS), 43)
        self.assertEqual(len(set(TELEGRAM_MENU_COMMAND_KEYS)), 43)
        self.assertIn("onboard", TELEGRAM_MENU_COMMAND_KEYS)

    def test_all_commands_have_menu_description(self):
        _ensure_usage_cache()
        for key in TELEGRAM_MENU_COMMAND_KEYS:
            self.assertIn("menu_description", USAGE[key])
            self.assertTrue(USAGE[key]["menu_description"].strip())

    def test_all_bot_commands_have_section_prefix(self):
        for lang in ("de", "en"):
            commands = all_bot_commands(lang)
            self.assertEqual(len(commands), 43)
            for entry in commands:
                self.assertIn("·", entry["description"])
                self.assertLessEqual(len(entry["description"]), 256)
            self.assertTrue(any(c["command"] == "onboard" for c in commands))

    def test_english_descriptions_differ_from_german(self):
        de = {c["command"]: c["description"] for c in all_bot_commands("de")}
        en = {c["command"]: c["description"] for c in all_bot_commands("en")}
        self.assertNotEqual(de["buy"], en["buy"])
        self.assertIn("Trading", en["buy"])

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

        menu_cfg = {
            "enabled": True,
            "default_language": "de",
            "force_language": None,
            "button_text": "Menü",
        }
        mock_bot_cfg = MagicMock()
        mock_bot_cfg.telegram_command_menu_config = menu_cfg
        mock_bot_cfg.observability_config = {"display_timezone": "UTC"}

        with patch("notifications.telegram_commands.command_menu.requests.post", return_value=mock_resp) as mock_post, \
             patch("notifications.telegram_commands.command_menu.menu_button_payload", return_value={"type": "commands", "text": "Menü"}), \
             patch("notifications.telegram_commands.command_menu.send_main_section_keyboard", return_value=True), \
             patch("core.tenant_context.multi_tenant_enabled", return_value=False), \
             patch("core.config.get_bot_config", return_value=mock_bot_cfg):
            ok = register_bot_commands(token="test-token")

        self.assertTrue(ok)
        # de + en + default fallback + setChatMenuButton
        self.assertEqual(mock_post.call_count, 4)
        set_cmds = [c for c in mock_post.call_args_list if "/setMyCommands" in c[0][0]]
        self.assertEqual(len(set_cmds), 3)
        langs = [c[1]["json"].get("language_code") for c in set_cmds]
        self.assertEqual(sorted([l for l in langs if l]), ["de", "en"])
        self.assertIn("/setChatMenuButton", mock_post.call_args_list[-1][0][0])

    def test_register_commands_for_chat_uses_chat_scope(self):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.content = b'{"ok": true}'
        mock_resp.json.return_value = {"ok": True}
        sat_n = sum(len(keys) for _, keys in MENU_SECTIONS_SATELLITE)

        with patch("notifications.telegram_commands.command_menu.requests.post", return_value=mock_resp) as mock_post, \
             patch(
                 "notifications.telegram_commands.command_menu.menu_sections_for",
                 return_value=MENU_SECTIONS_SATELLITE,
             ), \
             patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "tok"}, clear=False):
            ok = register_commands_for_chat(999, lang="de", token="tok")

        self.assertTrue(ok)
        mock_post.assert_called_once()
        payload = mock_post.call_args[1]["json"]
        self.assertEqual(payload["scope"], {"type": "chat", "chat_id": 999})
        self.assertEqual(len(payload["commands"]), sat_n)
        self.assertFalse(any(c["command"] == "onboard" for c in payload["commands"]))
        self.assertFalse(any(c["command"] == "live_confirm" for c in payload["commands"]))

    def test_register_without_token_returns_false(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(register_bot_commands(token=None))

    def test_register_does_not_send_keyboard_by_default(self):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.content = b'{"ok": true}'
        mock_resp.json.return_value = {"ok": True}

        with patch("notifications.telegram_commands.command_menu.requests.post", return_value=mock_resp), \
             patch("notifications.telegram_commands.command_menu.menu_button_payload", return_value={"type": "commands", "text": "Menü"}), \
             patch("notifications.telegram_commands.command_menu.send_main_section_keyboard", return_value=True) as mock_keyboard:
            self.assertTrue(register_bot_commands(token="test-token"))
        mock_keyboard.assert_not_called()

    def test_register_can_send_keyboard_when_requested(self):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.content = b'{"ok": true}'
        mock_resp.json.return_value = {"ok": True}

        with patch("notifications.telegram_commands.command_menu.requests.post", return_value=mock_resp), \
             patch("notifications.telegram_commands.command_menu.menu_button_payload", return_value={"type": "commands", "text": "Menü"}), \
             patch("notifications.telegram_commands.command_menu.send_main_section_keyboard", return_value=True) as mock_keyboard:
            self.assertTrue(register_bot_commands(token="test-token", send_keyboard=True))
        mock_keyboard.assert_called_once()


if __name__ == "__main__":
    unittest.main()