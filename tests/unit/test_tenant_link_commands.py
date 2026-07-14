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
    @patch("notifications.telegram_commands.tenant_link_commands.send_telegram_message")
    @patch("notifications.telegram_commands.tenant_link_commands.link_tenant_owner_chat")
    def test_links_and_notifies_operator(self, mock_link, mock_send):
        os.environ["TELEGRAM_CHAT_ID"] = "111"
        mock_link.return_value = (True, "OK")
        handled, msg = tlc.try_link_tenant_from_start("/start henry", "222")
        self.assertTrue(handled)
        self.assertEqual(msg, "OK")
        mock_link.assert_called_once_with("henry", "222")
        mock_send.assert_called_once()
        os.environ.pop("TELEGRAM_CHAT_ID", None)


if __name__ == "__main__":
    unittest.main()