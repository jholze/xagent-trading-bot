import unittest

from unittest.mock import patch

from notifications.telegram_commands.menu_i18n import (
    back_label,
    build_help_message,
    build_section_help_message,
    command_hint,
    command_description,
    help_label,
    is_back_label,
    is_help_label,
    menu_button_label,
    prefixed_command_description,
    resolve_language,
    resolve_ui_language,
    section_title,
    set_user_language,
    title_to_section_id,
)


class TestMenuI18n(unittest.TestCase):
    def test_resolve_language(self):
        self.assertEqual(resolve_language("de-DE"), "de")
        self.assertEqual(resolve_language("de"), "de")
        self.assertEqual(resolve_language("en-US"), "en")
        self.assertEqual(resolve_language("fr"), "en")

    def test_command_description_both_langs(self):
        self.assertIn("Coins", command_description("list", "de"))
        self.assertIn("coins", command_description("list", "en").lower())

    def test_prefixed_description(self):
        de = prefixed_command_description("handel", "buy", "de")
        en = prefixed_command_description("handel", "buy", "en")
        self.assertTrue(de.startswith("Handel ·"))
        self.assertTrue(en.startswith("Trading ·"))

    def test_section_titles_differ(self):
        self.assertEqual(section_title("handel", "de"), "💰 Handel")
        self.assertEqual(section_title("handel", "en"), "💰 Trading")

    def test_title_to_section_bilingual(self):
        self.assertEqual(title_to_section_id("💰 Handel"), "handel")
        self.assertEqual(title_to_section_id("💰 Trading"), "handel")

    def test_back_label(self):
        self.assertTrue(is_back_label(back_label("de")))
        self.assertTrue(is_back_label(back_label("en")))

    def test_stale_back_labels_are_back(self):
        # #496: old reply-keyboard back still walks one level up.
        self.assertTrue(is_back_label("◀ Bereiche"))
        self.assertTrue(is_back_label("◀ Sections"))
        self.assertTrue(is_back_label(back_label("de")))
        self.assertTrue(is_back_label(back_label("en")))
        self.assertEqual(back_label("de"), "◀ Zurück")
        self.assertEqual(back_label("en"), "◀ Back")

    def test_set_user_language_context(self):
        from notifications.telegram_commands.menu_i18n import current_language

        set_user_language("en")
        self.assertEqual(current_language(), "en")
        set_user_language("de")

    def test_help_message_language(self):
        de = build_help_message("de")
        en = build_help_message("en")
        self.assertIn("Telegram-Befehle", de)
        self.assertIn("Telegram commands", en)
        self.assertNotEqual(de, en)

    # --- #398: /help is role-filtered (satellite vs operator) -----------------

    OPERATOR_ONLY = ("/onboard", "/live_confirm", "/hermes_run", "/sandbox")

    def _help_for_role(self, role: str, lang: str = "de") -> str:
        with patch(
            "notifications.telegram_commands.menu_commands.menu_role_for",
            return_value=role,
        ):
            return build_help_message(lang, chat_id="chat-398")

    def test_help_satellite_hides_operator_only_commands(self):
        for lang in ("de", "en"):
            text = self._help_for_role("satellite", lang)
            self.assertIn("/positions", text)
            self.assertIn("/sell", text)
            for cmd in self.OPERATOR_ONLY:
                self.assertNotIn(cmd, text, f"{cmd} leaked into satellite /help ({lang})")
            # Whole "tests"/"onboarding" sections vanish, not just their lines.
            self.assertNotIn("Sandbox", text)
            self.assertNotIn("Onboarding", text)

    def test_help_operator_keeps_operator_only_commands(self):
        for lang in ("de", "en"):
            text = self._help_for_role("operator", lang)
            self.assertIn("/positions", text)
            self.assertIn("/sell", text)
            for cmd in self.OPERATOR_ONLY:
                self.assertIn(cmd, text, f"{cmd} missing from operator /help ({lang})")

    def test_help_operator_matches_unfiltered_catalog(self):
        # Operator role == today's full catalog; chat_id must not change the output.
        with patch(
            "notifications.telegram_commands.menu_commands.menu_role_for",
            return_value="operator",
        ):
            self.assertEqual(build_help_message("de", chat_id="111"), build_help_message("de"))

    def test_help_satellite_keys_are_subset_of_satellite_menu(self):
        from notifications.telegram_commands.menu_commands import (
            MENU_SECTIONS_OPERATOR,
            MENU_SECTIONS_SATELLITE,
        )

        sat_keys = {k for _, keys in MENU_SECTIONS_SATELLITE for k in keys}
        op_only = {k for _, keys in MENU_SECTIONS_OPERATOR for k in keys} - sat_keys
        text = self._help_for_role("satellite")
        for key in op_only:
            self.assertNotIn(f"/{key}", text, f"operator-only /{key} in satellite help")

    def test_help_handle_passes_current_chat_id(self):
        from notifications.telegram_commands import help_commands

        with patch.object(help_commands, "current_chat_id", return_value="sat-chat-9"), \
             patch.object(help_commands, "build_help_message", return_value="HELP") as build, \
             patch.object(help_commands, "send_telegram_message") as send:
            self.assertTrue(help_commands.handle("/help"))
        build.assert_called_once_with(chat_id="sat-chat-9")
        send.assert_called_once_with("HELP")

    def test_hints_follow_language(self):
        self.assertIn("Beispiel", command_hint("add", "de"))
        self.assertIn("Example", command_hint("add", "en"))

    def test_menu_button_label(self):
        self.assertEqual(menu_button_label("de"), "Menü")
        self.assertEqual(menu_button_label("en"), "Menu")

    @patch("storage.tenant_registry.get_tenant", return_value={"defaults": {"ui_language": "de"}})
    def test_resolve_ui_language_prefers_tenant_default(self, _tenant):
        upd = {"message": {"from": {"language_code": "en-US"}}}
        self.assertEqual(resolve_ui_language(upd, "henry"), "de")

    def test_resolve_ui_language_operator_follows_telegram(self):
        upd = {"message": {"from": {"language_code": "en-US"}}}
        self.assertEqual(resolve_ui_language(upd, "default"), "en")

    def test_section_help_message(self):
        de = build_section_help_message("transparenz", "de")
        en = build_section_help_message("transparenz", "en")
        self.assertIn("/stack", de)
        self.assertIn("/lc", de)
        self.assertIn("LunarCrush", de)
        self.assertIn("/lc", en)
        self.assertIn("LunarCrush", en)

    def test_help_label(self):
        self.assertTrue(is_help_label(help_label("de")))
        self.assertTrue(is_help_label(help_label("en")))


if __name__ == "__main__":
    unittest.main()