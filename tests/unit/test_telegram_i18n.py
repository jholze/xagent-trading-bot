import unittest

from notifications.telegram_commands.menu_i18n import set_user_language
from notifications.telegram_i18n import money, signed_money, t


class TestTelegramI18n(unittest.TestCase):
    def test_de_portfolio_title(self):
        set_user_language("de")
        self.assertIn("Portfolio", t("portfolio_title"))
        self.assertIn("geladen", t("portfolio_loading_compact"))

    def test_en_portfolio_loading(self):
        set_user_language("en")
        self.assertIn("loading", t("portfolio_loading_compact").lower())
        self.assertIn("Could not load", t("portfolio_load_failed", error="x"))

    def test_format_kwargs(self):
        set_user_language("de")
        msg = t("portfolio_slots", full=3, max=24, lots=5, setup="HARVEST · eff=24")
        self.assertIn("3/24", msg)

    def test_money_helpers(self):
        self.assertEqual(money(1000), "1,000")
        self.assertEqual(signed_money(-12.5, decimals=1), "-12.5")
        self.assertEqual(signed_money(0), "+0")
        self.assertEqual(signed_money(0.0, decimals=1), "+0.0")

    def test_fallback_to_de_key(self):
        set_user_language("en")
        # unknown key returns key itself
        self.assertEqual(t("___no_such_key___"), "___no_such_key___")


if __name__ == "__main__":
    unittest.main()
