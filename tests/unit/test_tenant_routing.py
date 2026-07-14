"""Tests for shared-bot tenant routing by Telegram chat_id."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.tenant_context import DEFAULT_TENANT
from core.tenant_routing import (
    extract_chat_id_from_update,
    iter_price_cycle_tenants,
    resolve_incoming_tenant,
)


class TestExtractChatId(unittest.TestCase):
    def test_message_update(self):
        upd = {"message": {"chat": {"id": 12345}, "text": "hi"}}
        self.assertEqual(extract_chat_id_from_update(upd), "12345")

    def test_callback_update(self):
        upd = {"callback_query": {"message": {"chat": {"id": 99}}}}
        self.assertEqual(extract_chat_id_from_update(upd), "99")


class TestResolveIncomingTenant(unittest.TestCase):
    def setUp(self):
        os.environ["TELEGRAM_CHAT_ID"] = "111"
        os.environ["MULTI_TENANT_ENABLED"] = "1"

    def tearDown(self):
        os.environ.pop("TELEGRAM_CHAT_ID", None)
        os.environ.pop("MULTI_TENANT_ENABLED", None)

    @patch("core.tenant_routing.multi_tenant_enabled", return_value=True)
    @patch("storage.tenant_registry.find_tenant_by_owner_chat_id")
    def test_operator_always_default(self, mock_find, _mt):
        mock_find.return_value = {"tenant_id": "henry", "defaults": {"ledger_scope": "paper"}}
        route = resolve_incoming_tenant(chat_id="111")
        self.assertEqual(route.tenant_id, DEFAULT_TENANT)
        mock_find.assert_not_called()

    @patch("core.tenant_routing.multi_tenant_enabled", return_value=True)
    @patch("storage.tenant_registry.find_tenant_by_owner_chat_id")
    def test_henry_chat_routes_to_henry(self, mock_find, _mt):
        mock_find.return_value = {
            "tenant_id": "henry",
            "defaults": {"ledger_scope": "paper"},
            "telegram": {"owner_chat_id": "222"},
        }
        route = resolve_incoming_tenant(chat_id="222")
        self.assertEqual(route.tenant_id, "henry")
        self.assertEqual(route.owner_chat_id, "222")

    @patch("core.tenant_routing.multi_tenant_enabled", return_value=True)
    @patch("storage.tenant_registry.find_tenant_by_owner_chat_id", return_value=None)
    def test_unknown_chat_rejected(self, _find, _mt):
        route = resolve_incoming_tenant(chat_id="999888777")
        self.assertTrue(route.rejected)


class TestIterPriceCycleTenants(unittest.TestCase):
    @patch("core.tenant_routing.multi_tenant_enabled", return_value=False)
    def test_single_default_when_disabled(self, _mt):
        self.assertEqual(iter_price_cycle_tenants(), [DEFAULT_TENANT])

    @patch("core.tenant_routing.multi_tenant_enabled", return_value=True)
    @patch("storage.tenant_registry.list_active_tenants")
    def test_includes_active_with_owner_chat(self, mock_list, _mt):
        mock_list.return_value = [
            {"tenant_id": "henry", "status": "active", "telegram": {"owner_chat_id": "222"}},
            {"tenant_id": "ghost", "status": "active", "telegram": {"owner_chat_id": ""}},
        ]
        ids = iter_price_cycle_tenants()
        self.assertEqual(ids[0], DEFAULT_TENANT)
        self.assertIn("henry", ids)
        self.assertNotIn("ghost", ids)


if __name__ == "__main__":
    unittest.main()