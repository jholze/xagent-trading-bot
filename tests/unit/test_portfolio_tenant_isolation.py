"""Portfolio display must read each tenant's Mongo ledger, not shared RAM."""

from __future__ import annotations

import os
import sys
import unittest
from decimal import Decimal
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.tenant_context import DEFAULT_TENANT, tenant_context
from notifications.telegram_commands.position_display import (
    _refresh_positions_for_snapshot,
    resolve_portfolio_context,
)
from strategies.positions import (
    _ensure_store,
    get_key,
    list_active_positions,
    list_active_positions_from_ledger,
    positions,
)


class TestPortfolioTenantIsolation(unittest.TestCase):
    def setUp(self):
        from strategies.positions import clear_positions_memory, _position_stores

        clear_positions_memory()
        clear_positions_memory(tenant_id="henry")
        _position_stores.clear()

    def _pollute_default_ram(self):
        key = get_key("SPCX/USDT", "1h")
        positions[key] = {
            "amount": Decimal("100"),
            "peak_amount": 100.0,
            "sold_percent": 0.0,
            "average_entry": 100.0,
            "realized_pnl": 0.0,
            "last_buy_price": 100.0,
            "recent_high": 110.0,
        }

    @patch("strategies.positions.load_positions_document")
    @patch("services.ledger_sync._build_positions_snapshot_from_orders")
    def test_ledger_read_ignores_polluted_default_ram(self, mock_orders, mock_cache):
        self._pollute_default_ram()
        mock_orders.return_value = {}
        mock_cache.return_value = {"positions": {}}

        with tenant_context("henry", scope="demo"):
            active = list_active_positions_from_ledger(scope="demo", tenant_id="henry")
            self.assertEqual(active, [])

    @patch("strategies.positions.load_positions_document")
    @patch("services.ledger_sync._build_positions_snapshot_from_orders")
    def test_refresh_snapshot_uses_ledger_not_active_key(self, mock_orders, mock_cache):
        self._pollute_default_ram()
        mock_orders.return_value = {}
        mock_cache.return_value = {"positions": {}}

        active = _refresh_positions_for_snapshot(tenant_id="henry", scope="demo")
        self.assertEqual(active, [])

    @patch("strategies.positions.load_positions_document")
    @patch("services.ledger_sync._build_positions_snapshot_from_orders")
    @patch("data_manager.load_orders")
    @patch("data_manager.load_trade_history_document")
    def test_resolve_portfolio_context_henry_cash_from_orders(
        self, mock_history, mock_load_orders, mock_order_snap, mock_cache
    ):
        mock_order_snap.return_value = {}
        mock_cache.return_value = {"positions": {}}
        mock_load_orders.return_value = {"orders": []}
        mock_history.return_value = {
            "virtual_balance": 100_000.0,
            "realized_pnl": 0.0,
            "trades": [],
        }

        with tenant_context("henry", scope="demo"):
            ctx = resolve_portfolio_context(tenant_id="henry", scope="demo")

        self.assertEqual(ctx["cash_balance"], 100_000.0)

    def test_list_active_positions_uses_tenant_store_not_active_key(self):
        self._pollute_default_ram()
        henry_key = ("henry", "demo")
        _ensure_store(henry_key)

        with tenant_context("henry", scope="demo"):
            self.assertEqual(list_active_positions(tenant_id="henry", scope="demo"), [])
            self.assertEqual(len(list_active_positions(tenant_id=DEFAULT_TENANT, scope="demo")), 1)


if __name__ == "__main__":
    unittest.main()