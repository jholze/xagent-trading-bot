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

    def test_portfolio_pnl_uses_trade_realized_not_residual(self):
        pnl = portfolio_pnl_for_display(97_582.0, 100_000.0, -5_389.0, 3_938.0)
        self.assertAlmostEqual(pnl["total_pnl"], -2_418.0, places=0)
        self.assertAlmostEqual(pnl["unrealized"], -5_389.0, places=0)
        self.assertAlmostEqual(pnl["trade_realized"], 3_938.0, places=0)
        self.assertNotAlmostEqual(pnl["trade_realized"], pnl["total_pnl"] - pnl["unrealized"], places=0)

    def test_format_portfolio_summary_matches_nav_and_shows_trade_gewinn(self):
        """Gesamt-PnL from NAV; Trade-Gewinn from ledger realized_pnl, not residual."""
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
        self.assertIn("$-5389", msg)
        self.assertNotIn("Realisiert", msg)
        self.assertNotIn("$+2971", msg)
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
        self.assertIn("$+12.5", msg)
        self.assertIn("Trade-Gewinn", msg)


if __name__ == "__main__":
    unittest.main()