"""Tests for operator notification helper."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.operator_notify import notify_operator, notify_tenant_linked, resolve_operator_chat_id


class TestOperatorNotify(unittest.TestCase):
    def test_resolve_from_env(self):
        os.environ["TELEGRAM_CHAT_ID"] = "999"
        try:
            self.assertEqual(resolve_operator_chat_id(), "999")
        finally:
            os.environ.pop("TELEGRAM_CHAT_ID", None)

    @patch("core.operator_notify._send_telegram_direct", create=True)
    def test_notify_tenant_linked_skips_self(self, mock_send):
        os.environ["TELEGRAM_CHAT_ID"] = "111"
        try:
            with patch("telegram_notifier._send_telegram_direct", mock_send):
                self.assertFalse(notify_tenant_linked("henry", "111"))
                mock_send.assert_not_called()
        finally:
            os.environ.pop("TELEGRAM_CHAT_ID", None)

    @patch("telegram_notifier._send_telegram_direct", return_value=True)
    def test_notify_operator_direct(self, mock_send):
        os.environ["TELEGRAM_CHAT_ID"] = "111"
        try:
            self.assertTrue(notify_operator("hello"))
            mock_send.assert_called_once()
            self.assertEqual(mock_send.call_args[1]["chat_id"], "111")
        finally:
            os.environ.pop("TELEGRAM_CHAT_ID", None)


if __name__ == "__main__":
    unittest.main()