import unittest

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

    def test_hints_follow_language(self):
        self.assertIn("Beispiel", command_hint("add", "de"))
        self.assertIn("Example", command_hint("add", "en"))

    def test_menu_button_label(self):
        self.assertEqual(menu_button_label("de"), "Menü")
        self.assertEqual(menu_button_label("en"), "Menu")

    def test_section_help_message(self):
        de = build_section_help_message("transparenz", "de")
        en = build_section_help_message("transparenz", "en")
        self.assertIn("/lc", de)
        self.assertIn("LunarCrush", de)
        self.assertIn("/lc", en)
        self.assertIn("LunarCrush", en)

    def test_help_label(self):
        self.assertTrue(is_help_label(help_label("de")))
        self.assertTrue(is_help_label(help_label("en")))


if __name__ == "__main__":
    unittest.main()