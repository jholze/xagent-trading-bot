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

    def test_portfolio_pnl_unrealized_is_balance_residual(self):
        """Unrealisiert = Gesamt-PnL - Trade-Gewinn (negative values reconcile)."""
        pnl = portfolio_pnl_for_display(107_406.0, 100_000.0, 3_937.7)
        self.assertAlmostEqual(pnl["total_pnl"], 7_406.0, places=0)
        self.assertAlmostEqual(pnl["trade_realized"], 3_937.7, places=1)
        self.assertAlmostEqual(pnl["unrealized"], 3_468.3, places=1)
        self.assertAlmostEqual(
            pnl["trade_realized"] + pnl["unrealized"],
            pnl["total_pnl"],
            places=2,
        )

    def test_portfolio_pnl_negative_total_reconciles(self):
        pnl = portfolio_pnl_for_display(97_582.0, 100_000.0, 3_938.0)
        self.assertAlmostEqual(pnl["total_pnl"], -2_418.0, places=0)
        self.assertAlmostEqual(pnl["unrealized"], -6_356.0, places=0)
        self.assertAlmostEqual(pnl["trade_realized"] + pnl["unrealized"], pnl["total_pnl"], places=2)

    def test_format_portfolio_summary_matches_nav_and_shows_trade_gewinn(self):
        with patch("notifications.telegram_commands.position_display.initial_capital", return_value=100_000.0):
            msg = format_portfolio_summary(
                {"virtual_balance": 3_338.0, "realized_pnl": 3_938.0},
                total_unreal=-5_389.0,
                position_count=42,
                cash_balance=3_338.0,
                positions_market_value=94_244.0,
            )
        self.assertIn("$97,582", msg)
        self.assertIn("$-2418", msg)
        self.assertIn("Trade-Gewinn", msg)
        self.assertIn("$+3938", msg)
        self.assertIn("$-6356", msg)
        self.assertNotIn("Realisiert", msg)
        self.assertNotIn("$-5389", msg)

    def test_format_portfolio_summary_railway_positive_case(self):
        with patch("notifications.telegram_commands.position_display.initial_capital", return_value=100_000.0):
            msg = format_portfolio_summary(
                {"virtual_balance": 3_337.70, "realized_pnl": 3_937.7},
                total_unreal=-5_884.8,
                position_count=46,
                cash_balance=3_337.70,
                positions_market_value=104_068.30,
            )
        self.assertIn("$+7406", msg)
        self.assertIn("$+3937", msg)
        self.assertIn("$+3468", msg)
        self.assertNotIn("$-5885", msg)

    def test_format_portfolio_summary_flat_when_nav_equals_initial(self):
        with patch("notifications.telegram_commands.position_display.initial_capital", return_value=5_000.0):
            msg = format_portfolio_summary(
                {"virtual_balance": 4_911.0, "realized_pnl": 12.5},
                total_unreal=25.0,
                position_count=2,
                positions_market_value=89.0,
            )
        self.assertIn("$+0.0", msg)
        self.assertIn("$-12.5", msg)
        self.assertIn("$+12.5", msg)
        self.assertIn("Trade-Gewinn", msg)


if __name__ == "__main__":
    unittest.main()