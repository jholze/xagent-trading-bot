import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from notifications.terminal_dashboard import _portfolio_snapshot


class TestTerminalDashboardBaseline(unittest.TestCase):
    def test_portfolio_snapshot_uses_baseline_and_scope(self):
        with patch("notifications.terminal_dashboard.get_bot_config") as mock_cfg, \
             patch("data_manager.load_trade_history", return_value={
                 "virtual_balance": 40000.0,
                 "realized_pnl": 100.0,
                 "trades": [],
             }), \
             patch("notifications.terminal_dashboard.list_active_positions", return_value=[]), \
             patch("notifications.terminal_dashboard.initial_capital", return_value=50000.0) as mock_baseline, \
             patch("notifications.terminal_dashboard.resolve_ledger_scope", return_value="demo") as mock_scope:
            cfg = mock_cfg.return_value
            cfg.trading_mode = "paper"
            cfg.raw = {"trading_mode": "paper"}
            snap = _portfolio_snapshot("paper")
        mock_scope.assert_called_once_with("paper")
        mock_baseline.assert_called_once()
        self.assertEqual(snap["ledger_scope"], "demo")
        self.assertEqual(snap["initial_capital"], 50000.0)
        self.assertAlmostEqual(snap["pnl_pct"], 0.2, places=2)


if __name__ == "__main__":
    unittest.main()