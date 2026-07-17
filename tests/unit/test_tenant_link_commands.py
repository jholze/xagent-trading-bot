"""Tests for /start invite linking."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from notifications.telegram_commands import tenant_link_commands as tlc


class TestParseStartPayload(unittest.TestCase):
    def test_start_with_tenant(self):
        self.assertEqual(tlc.parse_start_payload("/start henry"), "henry")

    def test_start_with_bot_suffix(self):
        self.assertEqual(tlc.parse_start_payload("/start@MyBot henry"), "henry")

    def test_start_empty(self):
        self.assertEqual(tlc.parse_start_payload("/start"), "")


class TestTryLinkTenantFromStart(unittest.TestCase):
    @patch("notifications.telegram_commands.menu_commands.open_menu_for_chat", return_value=True)
    @patch("notifications.telegram_commands.menu_i18n.resolve_ui_language", return_value="de")
    @patch("notifications.telegram_commands.tenant_link_commands.notify_tenant_linked")
    @patch("notifications.telegram_commands.tenant_link_commands.link_tenant_owner_chat")
    def test_links_and_notifies_operator(self, mock_link, mock_notify, _lang, mock_open):
        mock_link.return_value = (True, "OK")
        handled, msg = tlc.try_link_tenant_from_start("/start henry", "222")
        self.assertTrue(handled)
        self.assertEqual(msg, "OK")
        mock_link.assert_called_once_with("henry", "222")
        mock_notify.assert_called_once_with("henry", "222")
        mock_open.assert_called_once_with("222", lang="de")


class TestBareStartOpensMenu(unittest.TestCase):
    @patch("notifications.telegram_commands.tenant_link_commands.send_telegram_message")
    @patch("notifications.telegram_commands.menu_commands.open_menu_for_chat", return_value=True)
    @patch("notifications.telegram_commands.menu_i18n.resolve_ui_language", return_value="de")
    @patch("core.tenant_routing.resolve_incoming_tenant")
    @patch("notifications.telegram_commands.command_context.current_chat_id", return_value="sat-chat-1")
    def test_linked_chat_opens_menu(self, _cid, mock_route, mock_lang, mock_open, mock_send):
        from types import SimpleNamespace

        mock_route.return_value = SimpleNamespace(rejected=False, tenant_id="henry")
        self.assertTrue(tlc.handle("/start"))
        mock_open.assert_called_once_with("sat-chat-1", lang="de")
        mock_send.assert_not_called()

    @patch("notifications.telegram_commands.tenant_link_commands.send_telegram_message")
    @patch("notifications.telegram_commands.menu_commands.open_menu_for_chat", return_value=True)
    @patch("core.tenant_routing.resolve_incoming_tenant")
    @patch("notifications.telegram_commands.command_context.current_chat_id", return_value="stranger")
    def test_unlinked_chat_gets_welcome(self, _cid, mock_route, mock_open, mock_send):
        from types import SimpleNamespace

        mock_route.return_value = SimpleNamespace(rejected=True, tenant_id="default")
        self.assertTrue(tlc.handle("/start"))
        mock_open.assert_not_called()
        mock_send.assert_called_once()
        self.assertIn("Willkommen", mock_send.call_args[0][0])


if __name__ == "__main__":
    unittest.main()