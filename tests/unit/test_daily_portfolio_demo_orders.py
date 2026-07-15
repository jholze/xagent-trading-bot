"""Demo stack daily stats must use filled orders, not live trade_history.trades."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from notifications.daily_portfolio import format_daily_nav_line, today_activity_stats


class TestDailyPortfolioDemoOrders(unittest.TestCase):
    @patch("data_manager.is_demo_mode", return_value=True)
    @patch("notifications.daily_portfolio.resolve_ledger_scope", return_value="demo")
    @patch("notifications.daily_portfolio.filled_orders_today")
    def test_today_activity_stats_from_orders(self, mock_orders, _scope, _demo):
        today = date.today().isoformat()
        mock_orders.return_value = [
            {"side": "buy", "timestamps": {"filled": f"{today}T09:00:00"}},
            {"side": "buy", "timestamps": {"filled": f"{today}T09:05:00"}},
            {"side": "sell", "pnl": 42.0, "timestamps": {"filled": f"{today}T09:10:00"}},
        ]
        buys, sells, realized, active = today_activity_stats("live")
        self.assertTrue(active)
        self.assertEqual(buys, 2)
        self.assertEqual(sells, 1)
        self.assertAlmostEqual(realized, 42.0)

    @patch("data_manager.is_demo_mode", return_value=True)
    @patch("notifications.daily_portfolio.today_activity_stats", return_value=(16, 10, 435.0, True))
    @patch("notifications.daily_portfolio.estimate_nav_at_day_start", return_value=98500.0)
    def test_format_daily_nav_line_demo_orders(self, _nav, _stats, _demo):
        line = format_daily_nav_line("live", total_value=102000.0)
        self.assertIn("16 Käufe / 10 Verkäufe", line)
        self.assertIn("$435", line)


if __name__ == "__main__":
    unittest.main()