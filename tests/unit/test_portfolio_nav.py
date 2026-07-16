import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.portfolio_baseline import nav_total_pnl, portfolio_pnl_for_display
from notifications.telegram_commands.position_display import format_portfolio_summary


class TestPortfolioNav(unittest.TestCase):
    def test_nav_total_pnl_from_value_minus_initial(self):
        self.assertAlmostEqual(nav_total_pnl(98_516.0, 100_000.0), -1_484.0, places=0)

    def test_wertzuwachs_differs_from_handel_sum(self):
        """Operator ledger: NAV up ~$7.5k but trade+MTM is negative."""
        pnl = portfolio_pnl_for_display(107_548.0, 100_000.0, 3_937.7, -5_724.6)
        self.assertAlmostEqual(pnl["total_pnl"], 7_548.0, places=0)
        self.assertAlmostEqual(pnl["handel_sum"], -1_786.9, places=0)
        self.assertGreater(pnl["total_pnl"], pnl["handel_sum"])

    def test_format_portfolio_summary_shows_wertzuwachs_and_handel(self):
        with patch("notifications.telegram_commands.position_display.initial_capital", return_value=100_000.0):
            msg = format_portfolio_summary(
                {"virtual_balance": 3_171.95, "realized_pnl": 3_937.7},
                total_unreal=-5_724.6,
                position_count=46,
                cash_balance=3_171.95,
                positions_market_value=104_376.05,
            )
        self.assertIn("Wertzuwachs", msg)
        self.assertIn("$+7548", msg)
        self.assertIn("Handel", msg)
        self.assertIn("$-1786", msg)
        self.assertIn("Verk.", msg)
        self.assertIn("Lots $-5725", msg)
        self.assertNotIn("Gesamt-PnL", msg)
        self.assertNotIn("Unrealisiert", msg)

    def test_format_portfolio_summary_flat_when_nav_equals_initial(self):
        with patch("notifications.telegram_commands.position_display.initial_capital", return_value=5_000.0):
            msg = format_portfolio_summary(
                {"virtual_balance": 4_911.0, "realized_pnl": 12.5},
                total_unreal=25.0,
                position_count=2,
                positions_market_value=89.0,
            )
        self.assertIn("$+0.0", msg)
        self.assertIn("Handel", msg)


if __name__ == "__main__":
    unittest.main()