"""#449 — satellite Transparenz is Warum?/Fragen/Morgen; Technik nested; /why picker.

Does not rewrite #455 sellpos/lotsell. Catalog membership (MENU_SECTIONS_*) keeps
the same keys; only order and Mehr grouping change. Tests write nothing into data/.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from notifications.telegram_commands import command_context as ctx
from notifications.telegram_commands import decisions_commands, help_commands, router
from notifications.telegram_commands.menu_commands import (
    MENU_SECTIONS_OPERATOR,
    MENU_SECTIONS_SATELLITE,
    MORE_GROUPS_OPERATOR,
    MORE_GROUPS_SATELLITE,
    _section_reply_rows,
    more_groups_for,
    section_keys_for,
)
from notifications.telegram_commands.menu_i18n import (
    build_help_message,
    command_button_label,
    command_button_to_key,
    command_description,
    reload_menu_data,
    set_user_language,
)

MENU = "notifications.telegram_commands.menu_commands"
DC = "notifications.telegram_commands.decisions_commands"
FIRST_ROW = ("why", "ask", "morning")
TECHNIK_NEST = ("decisions", "stack", "hermes", "hermes_last", "cmc", "lc", "grid")
RESEARCH_ON_FIRST_ROW = ("cmc", "lc", "hermes", "grid", "stack")
CHAT = "449"


def _role(role: str):
    return patch(f"{MENU}.menu_role_for", return_value=role)


def _flat(rows):
    return [cell for row in rows for cell in row]


class TestTransparenzFirstRow449(unittest.TestCase):
    def setUp(self):
        set_user_language("de")
        reload_menu_data()

    def test_satellite_catalog_transparenz_starts_with_why_ask_morning(self):
        keys = dict(MENU_SECTIONS_SATELLITE)["transparenz"]
        self.assertEqual(tuple(keys[:3]), FIRST_ROW)
        for key in RESEARCH_ON_FIRST_ROW:
            self.assertNotIn(key, keys[:3])
            self.assertIn(key, keys)

    def test_satellite_technik_mehr_first_row_hides_research(self):
        keys = dict(MORE_GROUPS_SATELLITE)["technik"]
        self.assertEqual(tuple(keys[:3]), FIRST_ROW)
        self.assertEqual(tuple(keys[3:]), TECHNIK_NEST)
        for key in RESEARCH_ON_FIRST_ROW:
            self.assertNotIn(key, keys[:3])
        self.assertNotIn("why", dict(MORE_GROUPS_SATELLITE)["orders"])
        self.assertNotIn("ask", dict(MORE_GROUPS_SATELLITE)["orders"])

    def test_operator_keeps_full_transparenz_set_in_technik_nest(self):
        keys = dict(MORE_GROUPS_OPERATOR)["technik"]
        for key in FIRST_ROW + TECHNIK_NEST:
            self.assertIn(key, keys)
        op_catalog = {k for _, ks in MENU_SECTIONS_OPERATOR for k in ks}
        for key in FIRST_ROW + TECHNIK_NEST:
            self.assertIn(key, op_catalog)
        shown = [k for _, ks in more_groups_for(role="operator") for k in ks]
        for key in FIRST_ROW + TECHNIK_NEST:
            self.assertIn(key, shown)

    def test_satellite_technik_reply_row_uses_german_buttons_not_acronyms(self):
        with _role("satellite"):
            rows = _section_reply_rows("technik", "de", chat_id=999)
        first = rows[0]
        self.assertEqual(first, [
            command_button_label("why", "de"),
            command_button_label("ask", "de"),
            command_button_label("morning", "de"),
        ])
        self.assertEqual(first, ["❓ Warum?", "💬 Fragen", "☀️ Morgen"])
        flat = _flat(rows)
        for raw in ("/cmc", "/lc", "/hermes", "/grid", "/stack", "cmc", "lc"):
            self.assertNotIn(raw, first)
        self.assertIn(command_button_label("decisions", "de"), flat)
        self.assertEqual(command_button_label("decisions", "de"), "Letzte Entscheidung")
        self.assertNotEqual(command_button_label("decisions", "de"), "decisions")

    def test_technik_labels_exist_de_and_en(self):
        for key in FIRST_ROW + TECHNIK_NEST:
            de = command_button_label(key, "de") or command_description(key, "de")
            en = command_button_label(key, "en") or command_description(key, "en")
            self.assertTrue(de.strip(), f"missing DE label for {key}")
            self.assertTrue(en.strip(), f"missing EN label for {key}")
            self.assertNotEqual(de, key)
            self.assertNotEqual(en, key)
        self.assertNotEqual(
            command_button_label("why", "de"),
            command_button_label("why", "en"),
        )
        self.assertEqual(command_button_to_key("❓ Warum?"), "why")
        self.assertEqual(command_button_to_key("💬 Ask"), "ask")

    def test_section_keys_for_technik_matches_mehr_group(self):
        with _role("satellite"):
            self.assertEqual(
                section_keys_for("technik", chat_id=999),
                dict(MORE_GROUPS_SATELLITE)["technik"],
            )

    def test_warum_button_dispatches_why(self):
        from notifications.telegram_commands.menu_commands import handle_text

        with _role("satellite"), \
             patch("notifications.telegram_commands.router.dispatch_command", return_value=True) as dispatch:
            self.assertTrue(handle_text("❓ Warum?", chat_id=999))
        dispatch.assert_called_once_with("/why")


class TestSatelliteHelpNotADump449(unittest.TestCase):
    def setUp(self):
        set_user_language("de")
        reload_menu_data()

    def _help(self, role: str, *, catalog: bool = False, lang: str = "de") -> str:
        with patch(
            "notifications.telegram_commands.menu_commands.menu_role_for",
            return_value=role,
        ):
            return build_help_message(lang, chat_id="chat-449", catalog=catalog)

    def test_satellite_default_help_is_short_was_willst_du_tun(self):
        text = self._help("satellite")
        self.assertIn("Was willst du tun?", text)
        self.assertIn("/positions", text)
        self.assertIn("/sell", text)
        self.assertIn("/why", text)
        self.assertIn("/ask", text)
        self.assertIn("/morning", text)
        for cmd in ("/cmc", "/lc", "/hermes", "/grid", "/stack", "/onboard", "/sandbox"):
            self.assertNotIn(cmd, text)
        self.assertNotIn("Sandbox", text)
        self.assertNotIn("Onboarding", text)

    def test_satellite_help_all_is_role_filtered_catalog_not_operator_zoo(self):
        text = self._help("satellite", catalog=True)
        self.assertIn("/positions", text)
        self.assertIn("/why", text)
        for cmd in ("/onboard", "/live_confirm", "/hermes_run", "/sandbox"):
            self.assertNotIn(cmd, text)

    def test_operator_help_keeps_catalog(self):
        text = self._help("operator")
        self.assertIn("Telegram-Befehle", text)
        self.assertIn("/onboard", text)
        self.assertIn("/sandbox", text)

    def test_help_all_handle_passes_catalog_flag(self):
        with patch.object(help_commands, "current_chat_id", return_value="111"), \
             patch.object(help_commands, "build_help_message", return_value="CAT") as build, \
             patch.object(help_commands, "send_telegram_message"):
            self.assertTrue(help_commands.handle("/help all"))
        build.assert_called_once_with(chat_id="111", catalog=True)


class TestWhyPicker449(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        ctx._CONTEXT_FILE = Path(self.tmp.name) / "ctx.json"
        ctx.set_chat_id(CHAT)
        set_user_language("de")
        reload_menu_data()

    def tearDown(self):
        ctx.clear_context(CHAT)
        ctx.set_chat_id("")
        self.tmp.cleanup()

    def test_why_without_args_offers_tappable_recent_symbols(self):
        entries = [
            {"symbol": "NEAR/USDT"},
            {"symbol": "RAVE/USDT"},
        ]
        with patch(f"{DC}._load_decisions", return_value=entries), \
             patch(f"{DC}.send_telegram_buttons") as buttons, \
             patch(f"{DC}.send_telegram_message") as msg:
            self.assertTrue(decisions_commands.handle("/why"))
        msg.assert_not_called()
        buttons.assert_called_once()
        prompt, rows = buttons.call_args[0]
        self.assertIn("Warum", prompt)
        data = [b["callback_data"] for row in rows for b in row if "callback_data" in b]
        self.assertIn("whysym:RAVE", data)
        self.assertIn("whysym:NEAR", data)
        self.assertTrue(all(not d.startswith("sellpos:") and not d.startswith("lotsell:")
                            for d in data if d.startswith("whysym:") or d.startswith("sell")))
        self.assertEqual(ctx.get_context(CHAT)["command"], "why")

    def test_why_without_args_falls_back_to_positions(self):
        with patch(f"{DC}._load_decisions", return_value=[]), \
             patch("strategies.positions.list_active_positions", return_value=[
                 {"symbol": "ARIA/USDT", "timeframe": "4h", "amount": 1},
             ]), \
             patch(f"{DC}.send_telegram_buttons") as buttons:
            self.assertTrue(decisions_commands.handle("/why"))
        data = [b["callback_data"] for row in buttons.call_args[0][1] for b in row]
        self.assertIn("whysym:ARIA", data)

    def test_why_without_symbols_keeps_usage_and_arms_context(self):
        with patch(f"{DC}._load_decisions", return_value=[]), \
             patch("strategies.positions.list_active_positions", return_value=[]), \
             patch(f"{DC}.send_telegram_message") as msg, \
             patch(f"{DC}.send_telegram_buttons") as buttons:
            self.assertTrue(decisions_commands.handle("/why"))
        buttons.assert_not_called()
        msg.assert_called_once()
        self.assertIn("/why", msg.call_args[0][0])
        self.assertEqual(ctx.get_context(CHAT)["command"], "why")

    def test_whysym_callback_dispatches_why_not_sellpos(self):
        cb = {
            "id": "cq-why-449",
            "data": "whysym:RAVE",
            "message": {"chat": {"id": CHAT}},
        }
        with patch(f"{DC}.answer_callback_query"), \
             patch.object(decisions_commands, "_dispatch_why_async", return_value=True) as dispatch:
            self.assertTrue(decisions_commands.handle_callback(cb))
        dispatch.assert_called_once_with("RAVE")

    def test_whysym_prefix_is_not_sellpos_or_lotsell(self):
        data = "whysym:RAVE"
        self.assertTrue(data.startswith(decisions_commands.WHY_SYM_CALLBACK_PREFIX))
        self.assertFalse(data.startswith("sellpos:"))
        self.assertFalse(data.startswith("lotsell:"))
        self.assertFalse(data.startswith("poswhy:"))

    def test_router_routes_whysym_before_trading(self):
        why_cb = {"id": "1", "data": "whysym:NEAR", "message": {"chat": {"id": CHAT}}}
        with patch("notifications.telegram_commands.command_context.handle_callback", return_value=False), \
             patch.object(router.menu_commands, "handle_callback", return_value=False), \
             patch.object(router.decisions_commands, "handle_callback", return_value=True) as why, \
             patch.object(router.trading_commands, "handle_callback") as trade:
            self.assertTrue(router.dispatch_callback(why_cb))
        why.assert_called_once_with(why_cb)
        trade.assert_not_called()


if __name__ == "__main__":
    unittest.main()
