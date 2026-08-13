"""Tests for headless tenants: run the price cycle without a bound Telegram chat."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.tenant_context import current_tenant_context
from core.tenant_routing import tenant_cycle_context
from telegram_notifier import _headless_tenant_tag


class TestTenantCycleContextHeadless(unittest.TestCase):
    @patch("strategies.positions.activate_tenant_positions")
    @patch("storage.tenant_registry.get_tenant")
    def test_headless_flag_propagates_without_owner_chat(self, mock_get, _activate):
        mock_get.return_value = {
            "tenant_id": "ctexp",
            "status": "active",
            "telegram": {"owner_chat_id": "", "headless": True},
        }
        with tenant_cycle_context("ctexp"):
            ctx = current_tenant_context()
            self.assertTrue(ctx.headless)
            self.assertEqual(ctx.tenant_id, "ctexp")

    @patch("strategies.positions.activate_tenant_positions")
    @patch("storage.tenant_registry.get_tenant")
    def test_non_headless_tenant_unaffected(self, mock_get, _activate):
        mock_get.return_value = {
            "tenant_id": "henry",
            "status": "active",
            "telegram": {"owner_chat_id": "222"},
        }
        with tenant_cycle_context("henry"):
            ctx = current_tenant_context()
            self.assertFalse(ctx.headless)


class TestHeadlessNotificationTag(unittest.TestCase):
    @patch("strategies.positions.activate_tenant_positions")
    @patch("storage.tenant_registry.get_tenant")
    def test_tag_present_for_headless_tenant(self, mock_get, _activate):
        mock_get.return_value = {
            "tenant_id": "ctexp",
            "status": "active",
            "telegram": {"owner_chat_id": "", "headless": True},
        }
        with tenant_cycle_context("ctexp"):
            self.assertEqual(_headless_tenant_tag(), "[ctexp] ")

    @patch("strategies.positions.activate_tenant_positions")
    @patch("storage.tenant_registry.get_tenant")
    def test_tag_absent_for_bound_tenant(self, mock_get, _activate):
        mock_get.return_value = {
            "tenant_id": "henry",
            "status": "active",
            "telegram": {"owner_chat_id": "222"},
        }
        with tenant_cycle_context("henry"):
            self.assertEqual(_headless_tenant_tag(), "")

    def test_tag_absent_outside_tenant_context(self):
        self.assertEqual(_headless_tenant_tag(), "")


if __name__ == "__main__":
    unittest.main()
