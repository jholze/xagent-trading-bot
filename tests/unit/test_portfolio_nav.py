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

    def test_portfolio_pnl_honest_mtm_and_nav_residual(self):
        """Railway operator ledger: MTM negative, trade positive, rest explains NAV."""
        pnl = portfolio_pnl_for_display(107_548.0, 100_000.0, 3_937.7, -5_724.6)
        self.assertAlmostEqual(pnl["total_pnl"], 7_548.0, places=0)
        self.assertAlmostEqual(pnl["trade_realized"], 3_937.7, places=1)
        self.assertAlmostEqual(pnl["open_lots_mtm"], -5_724.6, places=1)
        self.assertAlmostEqual(pnl["nav_residual"], 9_334.9, places=0)
        self.assertAlmostEqual(
            pnl["trade_realized"] + pnl["open_lots_mtm"] + pnl["nav_residual"],
            pnl["total_pnl"],
            places=0,
        )

    def test_format_portfolio_summary_shows_open_lots_not_fake_unrealized(self):
        with patch("notifications.telegram_commands.position_display.initial_capital", return_value=100_000.0):
            msg = format_portfolio_summary(
                {"virtual_balance": 3_171.95, "realized_pnl": 3_937.7},
                total_unreal=-5_724.6,
                position_count=46,
                cash_balance=3_171.95,
                positions_market_value=104_376.05,
            )
        self.assertIn("$+7548", msg)
        self.assertIn("Trade-Gewinn", msg)
        self.assertIn("$+3937", msg)
        self.assertIn("Offene Lots", msg)
        self.assertIn("$-5724", msg)
        self.assertIn("Portfolio-Rest", msg)
        self.assertIn("$+9334", msg)
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
        self.assertIn("$+25.0", msg)
        self.assertIn("$+12.5", msg)
        self.assertIn("Offene Lots", msg)


if __name__ == "__main__":
    unittest.main()