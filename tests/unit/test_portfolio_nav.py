import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.portfolio_baseline import nav_total_pnl, split_nav_pnl_for_display
from notifications.telegram_commands.position_display import format_portfolio_summary


class TestPortfolioNav(unittest.TestCase):
    def test_nav_total_pnl_from_value_minus_initial(self):
        self.assertAlmostEqual(nav_total_pnl(98_516.0, 100_000.0), -1_484.0, places=0)

    def test_split_nav_pnl_components_sum_to_total(self):
        pnl = split_nav_pnl_for_display(98_516.0, 100_000.0, -9_223.0)
        self.assertAlmostEqual(pnl["total_pnl"], -1_484.0, places=0)
        self.assertAlmostEqual(pnl["realized"], pnl["total_pnl"] - pnl["unrealized"], places=6)
        self.assertAlmostEqual(pnl["realized"] + pnl["unrealized"], pnl["total_pnl"], places=6)

    def test_format_portfolio_summary_matches_nav_not_ledger_realized(self):
        """Gesamt-PnL must follow NAV; ledger realized_pnl alone must not drive the headline."""
        with patch("notifications.telegram_commands.position_display.initial_capital", return_value=100_000.0):
            msg = format_portfolio_summary(
                {"virtual_balance": 15_247.76, "realized_pnl": 10_638.48},
                total_unreal=-9_223.0,
                position_count=41,
                cash_balance=15_247.76,
                positions_market_value=83_268.0,
            )
        self.assertIn("$98,516", msg)
        self.assertIn("$-1484", msg)
        self.assertIn("$+7738", msg)
        self.assertNotIn("$+1,415", msg)
        self.assertNotIn("$+10638", msg)

    def test_format_portfolio_summary_flat_when_nav_equals_initial(self):
        with patch("notifications.telegram_commands.position_display.initial_capital", return_value=5_000.0):
            msg = format_portfolio_summary(
                {"virtual_balance": 4_911.0, "realized_pnl": 12.5},
                total_unreal=25.0,
                position_count=2,
                positions_market_value=89.0,
            )
        self.assertIn("$+0.0", msg)
        self.assertIn("$+25.0", msg)
        self.assertIn("$-25.0", msg)


if __name__ == "__main__":
    unittest.main()