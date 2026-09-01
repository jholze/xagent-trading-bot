"""Demo stack daily stats must use filled orders, not live trade_history.trades."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from notifications.daily_portfolio import format_daily_nav_line, today_activity_stats


class TestDailyPortfolioDemoOrders(unittest.TestCase):
    @patch("data_manager.is_demo_mode", return_value=True)
    @patch("notifications.daily_portfolio.resolve_ledger_scope", return_value="demo")
    @patch("notifications.daily_portfolio.is_simulated_trading", return_value=True)
    @patch("services.order_service.OrderService")
    def test_today_activity_stats_from_orders(self, mock_svc_cls, _sim, _scope, _demo):
        mock_svc = MagicMock()
        mock_svc.stats_day_filled_fast.return_value = {
            "filled": 3,
            "buys": 2,
            "sells": 1,
            "buy_usdt": 100.0,
            "sell_usdt": 50.0,
            "realized_pnl": 42.0,
            "sell_wins": 1,
            "sell_losses": 0,
        }
        mock_svc_cls.return_value = mock_svc
        buys, sells, realized, active = today_activity_stats("live")
        self.assertTrue(active)
        self.assertEqual(buys, 2)
        self.assertEqual(sells, 1)
        self.assertAlmostEqual(realized, 42.0)
        mock_svc.stats_day_filled_fast.assert_called_once()
        mock_svc.stats_day_filled.assert_not_called()

    @patch("data_manager.is_demo_mode", return_value=True)
    @patch("notifications.daily_portfolio.is_simulated_trading", return_value=True)
    @patch("services.order_service.OrderService")
    def test_today_activity_shares_order_service_window(self, mock_svc_cls, _sim, _demo):
        """Portfolio day counts must come from OrderService (same as /orders)."""
        mock_svc = MagicMock()
        mock_svc.stats_day_filled_fast.return_value = {
            "filled": 26,
            "buys": 16,
            "sells": 10,
            "realized_pnl": 556.0,
            "buy_usdt": 0,
            "sell_usdt": 0,
            "sell_wins": 9,
            "sell_losses": 1,
        }
        mock_svc_cls.return_value = mock_svc
        with patch("notifications.daily_portfolio.resolve_ledger_scope", return_value="demo"):
            buys, sells, realized, active = today_activity_stats("demo")
        self.assertEqual((buys, sells, realized, active), (16, 10, 556.0, True))
        mock_svc_cls.assert_called_with("demo")

    @patch("data_manager.is_demo_mode", return_value=True)
    @patch("notifications.daily_portfolio.today_activity_stats", return_value=(16, 10, 435.0, True))
    @patch("notifications.daily_portfolio.estimate_nav_at_day_start", return_value=98500.0)
    def test_format_daily_nav_line_demo_orders(self, _nav, _stats, _demo):
        line = format_daily_nav_line("live", total_value=102000.0)
        self.assertIn("16 Käufe / 10 Verkäufe", line)
        self.assertIn("$435", line)


if __name__ == "__main__":
    unittest.main()