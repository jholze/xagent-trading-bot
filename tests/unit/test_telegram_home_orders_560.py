"""#560 — home Orders label opens the Orders group, not /orders."""

import unittest
from unittest.mock import patch

from notifications.telegram_commands.command_context import (
    clear_active_section,
    get_active_section,
)
from notifications.telegram_commands.menu_commands import (
    MORE_GROUPS_OPERATOR,
    MORE_GROUPS_SATELLITE,
    _main_reply_rows,
    handle_text,
)
from notifications.telegram_commands.menu_i18n import (
    home_inline,
    home_intro,
    home_label,
    more_label,
    section_title,
    set_user_language,
)

MENU = "notifications.telegram_commands.menu_commands"


def _role(role: str):
    return patch(f"{MENU}.menu_role_for", return_value=role)


def _flat(rows):
    return [cell for row in rows for cell in row]


class TestHomeOrdersOpensGroup(unittest.TestCase):
    CHAT = 5600

    def setUp(self):
        set_user_language("de")
        clear_active_section(self.CHAT)

    def tearDown(self):
        clear_active_section(self.CHAT)
        set_user_language("de")

    def test_home_orders_label_opens_group_without_dispatch(self):
        for role in ("satellite", "operator"):
            for lang in ("de", "en"):
                with self.subTest(role=role, lang=lang):
                    set_user_language(lang)
                    clear_active_section(self.CHAT)
                    with _role(role), \
                         patch(f"{MENU}.send_reply_keyboard", return_value=True) as send, \
                         patch("notifications.telegram_commands.router.dispatch_command") as dispatch:
                        opened = handle_text(home_label("orders", lang), chat_id=self.CHAT)
                    self.assertTrue(opened)
                    dispatch.assert_not_called()
                    text, rows = send.call_args[0][0], send.call_args[0][1]
                    cells = _flat(rows)
                    self.assertIn(section_title("orders", lang), text)
                    self.assertIn("/orders", cells)
                    self.assertNotIn(home_label("orders", lang), cells)
                    self.assertEqual(get_active_section(self.CHAT), "orders")

    def test_orders_group_stays_on_mehr_for_both_roles(self):
        self.assertEqual(
            dict(MORE_GROUPS_SATELLITE)["orders"],
            ["orders", "plan", "risk"],
        )
        self.assertEqual(
            dict(MORE_GROUPS_OPERATOR)["orders"],
            ["positions_full", "orders", "orders_blocked", "orders_month", "plan", "risk"],
        )

    def test_home_keyboard_is_two_rows_of_three(self):
        for role in ("satellite", "operator"):
            with self.subTest(role=role), _role(role):
                rows = _main_reply_rows("de", chat_id=self.CHAT)
            self.assertEqual(
                rows,
                [
                    ["📊 Positionen", "📑 Orders", "💸 Verkaufen"],
                    ["🛒 Kaufen", "⏸ Pause", more_label("de")],
                ],
            )

    def test_home_copy_follows_new_order_and_puts_help_under_more(self):
        self.assertIn("Positionen, Orders, Verkaufen, Kaufen, Pause", home_intro("de"))
        self.assertIn("Hilfe unter", home_intro("de"))
        self.assertIn("Positionen, Orders, Verkaufen, Kaufen, Pause", home_inline("de"))
        self.assertIn("Hilfe unter Mehr", home_inline("de"))
        self.assertIn("Positions, Orders, Sell, Buy, Pause", home_intro("en"))
        self.assertIn("Help is under", home_intro("en"))
        self.assertIn("Positions, Orders, Sell, Buy, Pause", home_inline("en"))
        self.assertIn("Help is under More", home_inline("en"))
        self.assertNotEqual(section_title("orders", "de"), "📑 Orders")
        self.assertEqual(section_title("orders", "de"), "📑 Orders & Berichte")
        self.assertEqual(section_title("orders", "en"), "📑 Orders & Reports")
