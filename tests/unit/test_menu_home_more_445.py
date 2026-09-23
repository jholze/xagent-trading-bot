"""#445 — home keyboard vs nested ➕ Mehr (satellite + operator).

Catalog membership (MENU_SECTIONS_*) is untouched; this only checks *where*
keys are shown: slim home row, satellite ☰ = home set, Mehr groups reachable
by tapping, and every catalog key still reachable for both roles.
"""

import unittest
from unittest.mock import MagicMock, patch

from notifications.telegram_commands.command_context import (
    clear_active_section,
    get_active_section,
    set_active_section,
)
from notifications.telegram_commands.command_menu import register_commands_for_chat
from notifications.telegram_commands.menu_commands import (
    HOME_KEYS,
    MENU_SECTIONS_OPERATOR,
    MENU_SECTIONS_SATELLITE,
    MORE_GROUPS_OPERATOR,
    MORE_GROUPS_SATELLITE,
    MORE_SECTION_ID,
    _home_keyboard,
    _main_reply_rows,
    _more_reply_rows,
    _section_reply_rows,
    handle_callback,
    handle_text,
    home_keys_for,
    more_groups_for,
    section_keys_for,
    slash_sections_for,
)
from notifications.telegram_commands.menu_i18n import (
    _load_menu_data,
    back_label,
    build_section_help_message,
    help_label,
    home_label,
    home_label_to_key,
    is_more_label,
    more_label,
    section_title,
    set_user_language,
)

MENU = "notifications.telegram_commands.menu_commands"
NOT_ON_HOME = ("panic", "short", "cover", "hermes", "cmc", "reload")


def _role(role: str):
    return patch(f"{MENU}.menu_role_for", return_value=role)


def _flat(rows):
    return [cell for row in rows for cell in row]


class TestHomeKeyboard(unittest.TestCase):
    def setUp(self):
        set_user_language("de")

    def test_satellite_home_has_at_most_seven_keys(self):
        with _role("satellite"):
            keys = home_keys_for(chat_id=999)
            cells = _flat(_main_reply_rows(chat_id=999))
        self.assertLessEqual(len(keys), 7)
        self.assertEqual(len(cells), len(keys))
        for key in NOT_ON_HOME:
            self.assertNotIn(key, keys)
            self.assertNotIn(f"/{key}", cells)
        self.assertEqual(
            tuple(keys),
            ("positions", "orders", "sell", "buy", "pause", "menu"),
        )

    def test_operator_home_is_the_same_slim_row(self):
        self.assertEqual(
            HOME_KEYS,
            ["positions", "orders", "sell", "buy", "pause", "menu"],
        )
        with _role("operator"):
            keys = home_keys_for(chat_id=111)
        self.assertLessEqual(len(keys), 7)
        self.assertEqual(keys, HOME_KEYS)
        for key in NOT_ON_HOME + ("onboard", "sandbox", "live_confirm"):
            self.assertNotIn(key, keys)

    def test_home_labels_de_en(self):
        with _role("satellite"):
            de = _flat(_main_reply_rows("de", chat_id=999))
            en = _flat(_main_reply_rows("en", chat_id=999))
        self.assertIn(home_label("buy", "de"), de)
        self.assertIn(home_label("buy", "en"), en)
        self.assertNotEqual(home_label("buy", "de"), home_label("buy", "en"))
        self.assertNotIn(help_label("de"), de)
        self.assertNotIn(help_label("en"), en)
        self.assertEqual(home_label("orders", "de"), "📑 Orders")
        self.assertEqual(home_label("orders", "en"), "📑 Orders")
        self.assertIn(home_label("orders", "de"), de)
        self.assertIn(home_label("orders", "en"), en)
        self.assertNotEqual(home_label("orders", "de"), section_title("orders", "de"))
        self.assertNotEqual(home_label("orders", "en"), section_title("orders", "en"))
        self.assertIn(more_label("de"), de)
        self.assertIn(more_label("en"), en)
        self.assertTrue(is_more_label(more_label("de")))
        self.assertTrue(is_more_label(more_label("en")))
        # No raw slash fallbacks on the home row.
        self.assertFalse(any(c.startswith("/") for c in de + en))

    def test_home_label_resolves_in_both_languages(self):
        self.assertEqual(home_label_to_key(home_label("sell", "de")), "sell")
        self.assertEqual(home_label_to_key(home_label("sell", "en")), "sell")
        self.assertEqual(home_label_to_key(home_label("pause", "en")), "pause")
        self.assertIsNone(home_label_to_key("/sell"))


class TestSlashList(unittest.TestCase):
    def _post_ok(self):
        resp = MagicMock()
        resp.ok = True
        resp.content = b'{"ok": true}'
        resp.json.return_value = {"ok": True}
        return resp

    def test_satellite_set_my_commands_matches_home_set(self):
        with patch("notifications.telegram_commands.command_menu.requests.post", return_value=self._post_ok()) as post, \
             patch("notifications.telegram_commands.command_menu.menu_role_for", return_value="satellite"):
            self.assertTrue(register_commands_for_chat(999, lang="de", token="tok"))
        payload = post.call_args[1]["json"]
        self.assertEqual(payload["scope"], {"type": "chat", "chat_id": 999})
        cmds = [c["command"] for c in payload["commands"]]
        with _role("satellite"):
            self.assertEqual(cmds, home_keys_for(chat_id=999))
        self.assertLessEqual(len(cmds), 7)
        for key in NOT_ON_HOME:
            self.assertNotIn(key, cmds)
        # Slim list is not prefixed with a section name.
        for entry in payload["commands"]:
            self.assertNotIn("·", entry["description"])
            self.assertTrue(entry["description"].strip())

    def test_operator_set_my_commands_keeps_full_catalog(self):
        with patch("notifications.telegram_commands.command_menu.requests.post", return_value=self._post_ok()) as post, \
             patch("notifications.telegram_commands.command_menu.menu_role_for", return_value="operator"):
            self.assertTrue(register_commands_for_chat(111, lang="de", token="tok"))
        cmds = [c["command"] for c in post.call_args[1]["json"]["commands"]]
        self.assertEqual(cmds, [k for _, keys in MENU_SECTIONS_OPERATOR for k in keys])

    def test_slash_sections_for_roles(self):
        self.assertEqual(slash_sections_for(role="satellite"), [("home", HOME_KEYS)])
        self.assertEqual(slash_sections_for(role="operator"), MENU_SECTIONS_OPERATOR)


class TestMoreGroups(unittest.TestCase):
    def setUp(self):
        set_user_language("de")

    def test_every_catalog_key_reachable_once_per_role(self):
        for catalog, groups, role in (
            (MENU_SECTIONS_SATELLITE, MORE_GROUPS_SATELLITE, "satellite"),
            (MENU_SECTIONS_OPERATOR, MORE_GROUPS_OPERATOR, "operator"),
        ):
            catalog_keys = [k for _, keys in catalog for k in keys]
            shown = list(home_keys_for(role=role)) + [
                k for _, keys in more_groups_for(role=role) for k in keys
            ]
            # #560: orders stays on the home row and inside the Orders group.
            # help left HOME_KEYS; the Mehr footer is its reachability.
            duplicates = sorted(k for k in set(shown) if shown.count(k) != 1)
            self.assertEqual(duplicates, ["orders"], f"{role}: only orders may appear twice")
            self.assertEqual(shown.count("orders"), 2, f"{role}: orders shown twice")
            self.assertNotIn("help", shown)
            self.assertEqual(
                set(shown) | {"help"},
                set(catalog_keys),
                f"{role}: home ∪ Mehr | {{help}} != catalog",
            )
            self.assertIs(more_groups_for(role=role), groups)
            with _role(role):
                footer = _flat(_more_reply_rows(chat_id=999))
            self.assertIn(help_label("de"), footer)

    def test_satellite_groups_have_no_operator_only_keys(self):
        sat_keys = {k for _, keys in MORE_GROUPS_SATELLITE for k in keys}
        for key in ("onboard", "live_confirm", "live_cancel", "sandbox", "hermes_run", "tracktest", "testaccount"):
            self.assertNotIn(key, sat_keys)
        self.assertFalse(any(gid == "ops" for gid, _ in MORE_GROUPS_SATELLITE))
        self.assertTrue(any(gid == "ops" for gid, _ in MORE_GROUPS_OPERATOR))
        self.assertIn("onboard", dict(MORE_GROUPS_OPERATOR)["ops"])
        self.assertIn("sandbox", dict(MORE_GROUPS_OPERATOR)["ops"])

    def test_group_titles_exist_in_both_languages(self):
        data = _load_menu_data()
        for gid, _ in MORE_GROUPS_OPERATOR:
            for lang in ("de", "en"):
                self.assertIn(gid, data[lang]["sections"], f"{gid} missing in {lang}")
                self.assertTrue(section_title(gid, lang).strip())
        self.assertNotEqual(section_title("einstellungen", "de"), section_title("einstellungen", "en"))

    def test_more_keyboard_rows(self):
        with _role("satellite"):
            cells = _flat(_more_reply_rows(chat_id=999))
        for gid, _ in MORE_GROUPS_SATELLITE:
            self.assertIn(section_title(gid, "de"), cells)
        self.assertNotIn(section_title("ops", "de"), cells)
        self.assertIn(help_label("de"), cells)
        self.assertIn(back_label("de"), cells)

    def test_inline_home_lists_more_groups(self):
        with _role("satellite"):
            buttons = _flat(_home_keyboard(chat_id=999))
        self.assertEqual([b["callback_data"] for b in buttons], [f"menu:sec:{gid}" for gid, _ in MORE_GROUPS_SATELLITE])

    def test_section_keys_for_resolves_groups_and_catalog(self):
        with _role("satellite"):
            self.assertEqual(section_keys_for("shorts", chat_id=999), ["short", "cover"])
            self.assertEqual(section_keys_for("ops", chat_id=999), [])
            self.assertEqual(section_keys_for("handel", chat_id=999), dict(MENU_SECTIONS_SATELLITE)["handel"])
        with _role("operator"):
            self.assertIn("onboard", section_keys_for("ops", chat_id=111))
        rows = _flat(_section_reply_rows("shorts"))
        self.assertIn("/short", rows)
        self.assertIn("/cover", rows)
        self.assertIn(back_label("de"), rows)

    def test_section_help_for_group_reuses_catalog_items(self):
        # "orders" has no own section_help block → per-command items come from
        # the catalog sections (handel/transparenz) instead of an empty fallback.
        text = build_section_help_message("orders", "de", command_keys=["orders", "risk", "decisions"])
        self.assertIn(section_title("orders", "de"), text)
        for cmd in ("/orders", "/risk", "/decisions"):
            self.assertIn(cmd, text)
        data = _load_menu_data()
        usage = data["de"]["section_help"]["handel"]["items"]["orders"].get("usage")
        if usage:
            self.assertIn(usage, text)


class TestHandleText(unittest.TestCase):
    CHAT = 4450

    def setUp(self):
        set_user_language("de")
        clear_active_section(self.CHAT)

    def tearDown(self):
        clear_active_section(self.CHAT)
        set_user_language("de")  # #447: do not leave EN on the xdist worker

    def test_more_label_opens_group_list(self):
        with _role("satellite"), patch(f"{MENU}.send_reply_keyboard", return_value=True) as send:
            self.assertTrue(handle_text(more_label("de"), chat_id=self.CHAT))
        rows = send.call_args[0][1]
        self.assertIn(section_title("orders", "de"), _flat(rows))
        self.assertEqual(get_active_section(self.CHAT), MORE_SECTION_ID)

    def test_group_title_opens_group_keyboard_without_slash(self):
        with _role("satellite"), patch(f"{MENU}.send_reply_keyboard", return_value=True) as send:
            self.assertTrue(handle_text(section_title("shorts", "en"), chat_id=self.CHAT))
        self.assertIn("/short", _flat(send.call_args[0][1]))
        self.assertEqual(get_active_section(self.CHAT), "shorts")

    def test_operator_only_group_is_not_openable_for_satellite(self):
        with _role("satellite"), patch(f"{MENU}.send_reply_keyboard", return_value=True) as send:
            self.assertFalse(handle_text(section_title("ops", "de"), chat_id=self.CHAT))
        send.assert_not_called()

    def test_back_goes_one_level_up(self):
        with _role("satellite"), patch(f"{MENU}.send_reply_keyboard", return_value=True) as send:
            set_active_section(self.CHAT, "shorts")
            self.assertTrue(handle_text(back_label("de"), chat_id=self.CHAT))
            self.assertIn(section_title("orders", "de"), _flat(send.call_args[0][1]))  # → Mehr
            self.assertEqual(get_active_section(self.CHAT), MORE_SECTION_ID)
            self.assertTrue(handle_text(back_label("de"), chat_id=self.CHAT))
            self.assertIn(home_label("buy", "de"), _flat(send.call_args[0][1]))  # → home
            self.assertIsNone(get_active_section(self.CHAT))

    def _back_keyboard_rows(self, label: str, *, active: str | None, lang: str):
        set_user_language(lang)
        if active is None:
            clear_active_section(self.CHAT)
        else:
            set_active_section(self.CHAT, active)
        with _role("satellite"), patch(f"{MENU}.send_reply_keyboard", return_value=True) as send:
            handled = handle_text(label, chat_id=self.CHAT)
        self.assertTrue(handled, f"{label!r} must be handled as back")
        return send.call_args[0][1], get_active_section(self.CHAT)

    def test_stale_back_matches_current_back(self):
        # #496: stale ◀ Bereiche / ◀ Sections send the same keyboard as current back.
        # Group → Mehr, same keyboard as ◀ Zurück.
        current_rows, current_sec = self._back_keyboard_rows(
            back_label("de"), active="shorts", lang="de"
        )
        stale_rows, stale_sec = self._back_keyboard_rows(
            "◀ Bereiche", active="shorts", lang="de"
        )
        self.assertEqual(stale_rows, current_rows)
        self.assertEqual(stale_sec, current_sec)
        self.assertEqual(stale_sec, MORE_SECTION_ID)
        self.assertIn(section_title("orders", "de"), _flat(stale_rows))

        # Mehr / none → home, same keyboard as ◀ Back.
        current_rows, current_sec = self._back_keyboard_rows(
            back_label("en"), active=MORE_SECTION_ID, lang="en"
        )
        stale_rows, stale_sec = self._back_keyboard_rows(
            "◀ Sections", active=MORE_SECTION_ID, lang="en"
        )
        self.assertEqual(stale_rows, current_rows)
        self.assertEqual(stale_sec, current_sec)
        self.assertIsNone(stale_sec)
        self.assertIn(home_label("buy", "en"), _flat(stale_rows))

    def test_home_label_dispatches_command(self):
        with _role("satellite"), \
             patch("notifications.telegram_commands.router.dispatch_command", return_value=True) as dispatch:
            self.assertTrue(handle_text(home_label("buy", "de"), chat_id=self.CHAT))
            dispatch.assert_called_once_with("/buy")
            dispatch.reset_mock()
            self.assertTrue(handle_text(home_label("pause", "en"), chat_id=self.CHAT))
            dispatch.assert_called_once_with("/pause")

    def test_help_on_home_runs_full_help(self):
        with _role("satellite"), \
             patch("notifications.telegram_commands.router.dispatch_command", return_value=True) as dispatch:
            self.assertTrue(handle_text(help_label("de"), chat_id=self.CHAT))
        dispatch.assert_called_once_with("/help")

    def test_help_inside_group_sends_group_help(self):
        with _role("satellite"), \
             patch(f"{MENU}.send_telegram_message", return_value=True) as send, \
             patch("notifications.telegram_commands.router.dispatch_command") as dispatch:
            set_active_section(self.CHAT, "shorts")
            self.assertTrue(handle_text(help_label("de"), chat_id=self.CHAT))
        dispatch.assert_not_called()
        text = send.call_args[0][0]
        self.assertIn("/short", text)
        self.assertIn("/cover", text)

    def test_inline_group_callback_edits_message(self):
        cb = {"id": "cq445", "data": "menu:sec:shorts", "message": {"chat": {"id": self.CHAT}, "message_id": 7}}
        with _role("satellite"), \
             patch(f"{MENU}.answer_callback_query"), \
             patch(f"{MENU}.edit_telegram_message", return_value=True) as edit:
            self.assertTrue(handle_callback(cb))
        buttons = _flat(edit.call_args[1]["reply_markup"])
        self.assertIn("menu:run:short", [b["callback_data"] for b in buttons])
        self.assertIn("menu:home", [b["callback_data"] for b in buttons])


if __name__ == "__main__":
    unittest.main()
