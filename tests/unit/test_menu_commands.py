import unittest
from unittest.mock import patch

from notifications.telegram_commands.menu_commands import (
    MENU_SECTIONS,
    MENU_SECTIONS_SATELLITE,
    _home_keyboard,
    _section_reply_rows,
    all_menu_command_keys,
    handle,
    handle_callback,
    handle_text,
    menu_role_for,
    menu_sections_for,
    show_home,
)
from notifications.telegram_commands.menu_i18n import back_label, help_label, set_user_language


class TestMenuCommands(unittest.TestCase):
    def test_all_commands_in_sections(self):
        keys = all_menu_command_keys()
        self.assertEqual(len(keys), 43)
        self.assertIn("stack", keys)
        self.assertIn("positions_full", keys)
        self.assertIn("lc", keys)
        self.assertIn("sandbox_results", keys)
        self.assertIn("backtest_lock", keys)
        self.assertIn("onboard", keys)

    def test_seven_sections(self):
        self.assertEqual(len(MENU_SECTIONS), 7)

    def test_satellite_menu_hides_ops(self):
        sat_keys = [k for _, keys in MENU_SECTIONS_SATELLITE for k in keys]
        self.assertNotIn("onboard", sat_keys)
        self.assertNotIn("live_confirm", sat_keys)
        self.assertNotIn("sandbox", sat_keys)
        self.assertNotIn("hermes_run", sat_keys)
        self.assertIn("positions", sat_keys)
        self.assertEqual(len(MENU_SECTIONS_SATELLITE), 6)
        self.assertFalse(any(sid == "tests" for sid, _ in MENU_SECTIONS_SATELLITE))

    def test_menu_role_satellite_by_tenant(self):
        with patch("core.tenant_context.multi_tenant_enabled", return_value=True), \
             patch.dict("os.environ", {"TELEGRAM_CHAT_ID": "111"}, clear=False), \
             patch(
                 "storage.tenant_registry.find_tenant_by_owner_chat_id",
                 return_value={"tenant_id": "henry"},
             ):
            self.assertEqual(menu_role_for(chat_id="sat-chat-1"), "satellite")
            secs = menu_sections_for(chat_id="sat-chat-1")
            self.assertEqual(secs, MENU_SECTIONS_SATELLITE)

    def test_menu_role_operator_chat(self):
        with patch("core.tenant_context.multi_tenant_enabled", return_value=True), \
             patch.dict("os.environ", {"TELEGRAM_CHAT_ID": "111"}, clear=False):
            self.assertEqual(menu_role_for(chat_id="111"), "operator")

    def test_menu_role_fail_closed_on_registry_error(self):
        with patch("core.tenant_context.multi_tenant_enabled", return_value=True), \
             patch.dict("os.environ", {"TELEGRAM_CHAT_ID": "111"}, clear=False), \
             patch(
                 "storage.tenant_registry.find_tenant_by_owner_chat_id",
                 side_effect=RuntimeError("mongo down"),
             ):
            self.assertEqual(menu_role_for(chat_id="sat-chat-1"), "satellite")

    def test_home_keyboard_has_sections(self):
        with patch(
            "notifications.telegram_commands.menu_commands.menu_role_for",
            return_value="operator",
        ):
            buttons = [b for row in _home_keyboard() for b in row]
            self.assertEqual(len(buttons), 7)

    def test_home_keyboard_satellite_has_six_sections(self):
        with patch(
            "notifications.telegram_commands.menu_commands.menu_role_for",
            return_value="satellite",
        ):
            buttons = [b for row in _home_keyboard(chat_id=999) for b in row]
            self.assertEqual(len(buttons), 6)

    def test_section_reply_rows_include_commands_and_back(self):
        rows = _section_reply_rows("handel")
        flat = [cell for row in rows for cell in row]
        self.assertIn("/positions full", flat)
        self.assertIn("/buy", flat)
        self.assertIn(back_label("de"), flat)
        self.assertIn(help_label("de"), flat)

    def test_handle_menu_command(self):
        with patch("notifications.telegram_commands.menu_commands.send_main_section_keyboard", return_value=True), \
             patch("notifications.telegram_commands.menu_commands.send_telegram_buttons", return_value=True), \
             patch("notifications.telegram_commands.menu_commands._register_chat_commands_safe") as mock_reg, \
             patch("notifications.telegram_commands.command_context.current_chat_id", return_value="999"):
            self.assertTrue(handle("/menu"))
            mock_reg.assert_called()

    def test_show_home_registers_chat_commands(self):
        with patch("notifications.telegram_commands.menu_commands.send_main_section_keyboard", return_value=True), \
             patch("notifications.telegram_commands.menu_commands.send_telegram_buttons", return_value=True), \
             patch(
                 "notifications.telegram_commands.menu_commands._register_chat_commands_safe"
             ) as mock_reg:
            show_home(chat_id=42)
            mock_reg.assert_called_once()
            self.assertEqual(mock_reg.call_args[0][0], 42)

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
             patch("notifications.telegram_commands.menu_commands.menu_role_for", return_value="operator"), \
             patch("notifications.telegram_commands.router.dispatch_command", return_value=True) as mock_dispatch:
            self.assertTrue(handle_callback(cb))
            mock_dispatch.assert_called_once_with("/positions")

    def test_callback_run_dispatches_positions_full(self):
        cb = {"id": "cq3", "data": "menu:run:positions_full", "message": {"chat": {"id": 1}, "message_id": 2}}
        with patch("notifications.telegram_commands.menu_commands.answer_callback_query"), \
             patch("notifications.telegram_commands.menu_commands.menu_role_for", return_value="operator"), \
             patch("notifications.telegram_commands.router.dispatch_command", return_value=True) as mock_dispatch:
            self.assertTrue(handle_callback(cb))
            mock_dispatch.assert_called_once_with("/positions full")

    def test_satellite_callback_denies_operator_only_command(self):
        cb = {
            "id": "cq4",
            "data": "menu:run:live_confirm",
            "message": {"chat": {"id": 999}, "message_id": 2},
        }
        with patch("notifications.telegram_commands.menu_commands.answer_callback_query") as mock_ans, \
             patch("notifications.telegram_commands.menu_commands.menu_role_for", return_value="satellite"), \
             patch("notifications.telegram_commands.router.dispatch_command") as mock_dispatch:
            self.assertTrue(handle_callback(cb))
            mock_dispatch.assert_not_called()
            mock_ans.assert_called()


if __name__ == "__main__":
    unittest.main()