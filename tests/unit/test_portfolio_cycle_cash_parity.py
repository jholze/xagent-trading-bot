"""Cycle summary and /portfolio must show the same Sim USDT for a tenant."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from notifications.terminal_dashboard import _portfolio_snapshot
from notifications.telegram_commands.position_display import resolve_portfolio_context


class TestPortfolioCycleCashParity(unittest.TestCase):
    def test_snapshot_matches_portfolio_context_cash(self):
        mock_cfg = MagicMock()
        mock_cfg.raw = {
            "trading_mode": "live",
            "live": {"dry_run": True, "dry_run_enhanced": True, "simulated_balance_usdt": 100_000},
        }
        mock_cfg.trading_mode = "live"
        hist = {"virtual_balance": 999.0, "realized_pnl": 0.0, "trades": []}
        order_cash = 10_512.0

        patches = [
            patch("notifications.terminal_dashboard.get_bot_config", return_value=mock_cfg),
            patch("notifications.telegram_commands.position_display.get_bot_config", return_value=mock_cfg),
            patch("notifications.telegram_commands.position_display.load_trade_history_safe", return_value=hist),
            patch("notifications.terminal_dashboard.list_active_positions", return_value=[]),
            patch("notifications.telegram_commands.position_display._refresh_positions_for_snapshot"),
            patch("core.simulated_trading.is_simulated_trading", return_value=True),
            patch("core.simulated_trading.uses_order_ledger_cash", return_value=True),
            patch("core.simulated_trading.simulated_ledger_scope", return_value="demo"),
            patch("data_manager.resolve_sim_cash_balance", return_value=order_cash),
            patch("data_manager.resolve_sim_realized_pnl", return_value=250.0),
            patch(
                "notifications.telegram_commands.position_display._sim_order_ledger_bundle",
                return_value={
                    "history": hist,
                    "cash_balance": order_cash,
                    "trade_realized": 250.0,
                    "active": [],
                    "gate_holdings": None,
                    "filled_orders": [],
                },
            ),
            patch("data_manager.resolve_ledger_scope", return_value="demo"),
        ]
        for p in patches:
            p.start()
        try:
            snap = _portfolio_snapshot("live")
            ctx = resolve_portfolio_context(fast=True)
        finally:
            for p in reversed(patches):
                p.stop()

        self.assertAlmostEqual(float(snap["balance"]), order_cash, places=2)
        self.assertAlmostEqual(float(ctx["cash_balance"]), order_cash, places=2)
        self.assertEqual(snap["balance_label"], "Sim USDT")
        self.assertEqual(ctx["cash_label"], "Sim USDT")


if __name__ == "__main__":
    unittest.main()