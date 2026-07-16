import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.portfolio_baseline import (
    nav_total_pnl,
    portfolio_nav_breakdown,
    portfolio_pnl_for_display,
    reconcile_display_nav,
)
from notifications.telegram_commands.position_display import format_portfolio_summary


class TestPortfolioNav(unittest.TestCase):
    def test_nav_total_pnl_from_value_minus_initial(self):
        self.assertAlmostEqual(nav_total_pnl(98_516.0, 100_000.0), -1_484.0, places=0)

    def test_reconcile_display_nav_when_stale_cash_with_open_coins(self):
        """Stale virtual_balance $100k + $4.3k coins must not show +$4.3k Wertzuwachs."""
        fix = reconcile_display_nav(
            100_000.0, 100_000.0, 4_326.0, 0.0, -26.0,
        )
        self.assertTrue(fix["reconciled"])
        self.assertAlmostEqual(fix["total_value"], 99_974.0, places=0)
        self.assertAlmostEqual(fix["cash_balance"], 95_648.0, places=0)

    def test_format_portfolio_summary_stale_cash_matches_lots_pnl(self):
        with patch("notifications.telegram_commands.position_display.initial_capital", return_value=100_000.0):
            msg = format_portfolio_summary(
                {"virtual_balance": 100_000.0, "realized_pnl": 0.0},
                total_unreal=-26.0,
                position_count=2,
                cash_balance=100_000.0,
                positions_market_value=4_326.0,
                positions_cost_basis=4_352.0,
            )
        self.assertIn("$95,648", msg)
        self.assertIn("$99,974", msg)
        self.assertIn("$-26", msg)
        self.assertNotIn("$104,326", msg)
        self.assertNotIn("$+4326", msg)

    def test_wertzuwachs_equals_cash_delta_plus_coins(self):
        """Operator ledger: NAV PnL decomposes as cash change + coins MV, not Verk+Llots."""
        cash = 3_171.95
        coins = 104_376.05
        parts = portfolio_nav_breakdown(cash, 100_000.0, coins)
        self.assertAlmostEqual(parts["cash_delta"], cash - 100_000.0, places=0)
        self.assertAlmostEqual(parts["coins_contribution"], coins, places=0)
        self.assertAlmostEqual(parts["total_pnl"], 7_548.0, places=0)
        pnl = portfolio_pnl_for_display(cash + coins, 100_000.0, 3_937.7, -5_724.6)
        self.assertAlmostEqual(pnl["total_pnl"], parts["total_pnl"], places=0)

    def test_format_portfolio_summary_shows_nav_breakdown_not_handel_sum(self):
        with patch("notifications.telegram_commands.position_display.initial_capital", return_value=100_000.0):
            msg = format_portfolio_summary(
                {"virtual_balance": 3_171.95, "realized_pnl": 3_937.7},
                total_unreal=-5_724.6,
                position_count=46,
                cash_balance=3_171.95,
                positions_market_value=104_376.05,
            )
        self.assertIn("Coins", msg)
        self.assertIn("$104,376", msg)
        self.assertIn("Marktwert", msg)
        self.assertIn("Wertzuwachs", msg)
        self.assertIn("$+7548", msg)
        self.assertIn("Einstand", msg)
        self.assertIn("Marktwert", msg)
        self.assertIn("Δ vs Entry", msg)
        self.assertIn("$107,548</b> − Start", msg)
        self.assertIn("Verkäufe (realisiert)", msg)
        self.assertNotIn("Handel", msg)
        self.assertNotIn("Coins $+104", msg)
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
        self.assertIn("Verkäufe", msg)


if __name__ == "__main__":
    unittest.main()