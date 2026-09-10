"""Single-tenant Telegram owner allowlist (webhooks.auth + core.tenant_routing wiring).

Multi-tenant deployments already gate unknown senders via the tenant
registry (see TestResolveIncomingTenant.test_unknown_chat_rejected in
test_tenant_routing.py) -- these tests cover the previously-unguarded
gap: multi-tenancy OFF, where any sender used to be accepted unconditionally.
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.tenant_context import DEFAULT_TENANT
from core.tenant_routing import resolve_incoming_tenant
from webhooks.auth import telegram_allowed_chat_ids, telegram_sender_allowed


class TestTelegramSenderAllowed(unittest.TestCase):
    def test_rejects_when_no_owner_configured(self):
        env = {k: v for k, v in os.environ.items() if k not in ("TELEGRAM_CHAT_ID", "TELEGRAM_ALLOWED_CHAT_IDS")}
        with patch.dict(os.environ, env, clear=True):
            self.assertFalse(telegram_sender_allowed("123"))
            self.assertEqual(telegram_allowed_chat_ids(), set())

    def test_accepts_owner_and_extra(self):
        with patch.dict(
            os.environ,
            {"TELEGRAM_CHAT_ID": "111", "TELEGRAM_ALLOWED_CHAT_IDS": "222, 333"},
            clear=False,
        ):
            self.assertTrue(telegram_sender_allowed(111))
            self.assertTrue(telegram_sender_allowed("222"))
            self.assertTrue(telegram_sender_allowed("333"))
            self.assertFalse(telegram_sender_allowed("999"))

    def test_extra_ids_optional(self):
        env = {k: v for k, v in os.environ.items() if k != "TELEGRAM_ALLOWED_CHAT_IDS"}
        with patch.dict(os.environ, {**env, "TELEGRAM_CHAT_ID": "111"}, clear=True):
            self.assertEqual(telegram_allowed_chat_ids(), {"111"})


class TestResolveIncomingTenantSingleTenantAllowlist(unittest.TestCase):
    """The gap this ticket closes: resolve_incoming_tenant with multi-tenancy off."""

    @patch("core.tenant_routing.multi_tenant_enabled", return_value=False)
    def test_owner_allowed(self, _mt):
        with patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "111"}, clear=False):
            route = resolve_incoming_tenant(chat_id="111")
        self.assertFalse(route.rejected)
        self.assertEqual(route.tenant_id, DEFAULT_TENANT)

    @patch("core.tenant_routing.multi_tenant_enabled", return_value=False)
    def test_stranger_rejected(self, _mt):
        with patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "111"}, clear=False):
            route = resolve_incoming_tenant(chat_id="999")
        self.assertTrue(route.rejected)

    @patch("core.tenant_routing.multi_tenant_enabled", return_value=False)
    def test_extra_allowed_chat_id_accepted(self, _mt):
        with patch.dict(
            os.environ,
            {"TELEGRAM_CHAT_ID": "111", "TELEGRAM_ALLOWED_CHAT_IDS": "222"},
            clear=False,
        ):
            route = resolve_incoming_tenant(chat_id="222")
        self.assertFalse(route.rejected)

    @patch("core.tenant_routing.multi_tenant_enabled", return_value=False)
    def test_bootstrap_permissive_when_owner_unset(self, _mt):
        """Before TELEGRAM_CHAT_ID is ever set, stay permissive -- there'd be
        no other way to configure it via chat. Matches pre-fix behaviour for
        this one case; the fix only starts enforcing once an owner exists."""
        env = {k: v for k, v in os.environ.items() if k != "TELEGRAM_CHAT_ID"}
        with patch.dict(os.environ, env, clear=True):
            route = resolve_incoming_tenant(chat_id="anything")
        self.assertFalse(route.rejected)
        self.assertEqual(route.owner_chat_id, "anything")


class TestWebhookRouteSingleTenantAllowlist(unittest.TestCase):
    """End-to-end: the real POST / route, not just resolve_incoming_tenant."""

    def setUp(self):
        from aria_bot import app

        self.client = app.test_client()

    @patch("core.tenant_routing.multi_tenant_enabled", return_value=False)
    def test_stranger_ignored(self, _mt):
        with patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "111"}, clear=False), patch(
            "aria_bot.handle_telegram_command"
        ) as mock_cmd:
            resp = self.client.post(
                "/",
                json={"message": {"text": "/mode live", "chat": {"id": 999}}},
            )
        self.assertEqual(resp.status_code, 200)
        mock_cmd.assert_not_called()

    @patch("core.tenant_routing.multi_tenant_enabled", return_value=False)
    def test_owner_still_works(self, _mt):
        with patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "111"}, clear=False), patch(
            "aria_bot.handle_telegram_command"
        ) as mock_cmd:
            resp = self.client.post(
                "/",
                json={"message": {"text": "/mode", "chat": {"id": 111}}},
            )
        self.assertEqual(resp.status_code, 200)
        mock_cmd.assert_called_once()


if __name__ == "__main__":
    unittest.main()
