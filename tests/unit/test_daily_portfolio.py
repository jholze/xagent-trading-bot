import os
import sys
import unittest
from datetime import date
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from notifications.daily_portfolio import (
    format_daily_nav_line,
    realized_pnl_for,
    trades_today,
)
from notifications.telegram_commands.position_display import (
    _effective_sold_fraction,
    format_position_card,
    format_portfolio_summary,
)


class TestDailyPortfolio(unittest.TestCase):
    def test_effective_sold_fraction_uses_peak_amount(self):
        p = {"amount": 76.0, "peak_amount": 100.0, "sold_percent": 1.0}
        self.assertAlmostEqual(_effective_sold_fraction(p), 0.24, places=2)

    def test_position_card_shows_correct_sold_pct_with_peak(self):
        p = {
            "symbol": "XPL/USDT",
            "amount": 76.0,
            "peak_amount": 100.0,
            "average_entry": 1.0,
            "sold_percent": 1.0,
            "last_action": "SELL",
        }
        card = format_position_card(1, p, 1.0, numbered=True)
        self.assertIn("24%", card)
        self.assertNotIn("⚠️ 100%", card)

    def test_portfolio_summary_nav_minus_initial(self):
        msg = format_portfolio_summary(
            {"virtual_balance": 3952.19, "realized_pnl": -111.82},
            total_unreal=18.5,
            position_count=4,
            cash_balance=3952.19,
            positions_market_value=868.0,
        )
        self.assertIn("Gesamt-PnL", msg)
        self.assertIn("$-179.8", msg)

    def test_realized_pnl_for_sells_only(self):
        trades = [
            {"type": "BUY", "pnl": None},
            {"type": "SELL", "pnl": -50.0},
            {"type": "SELL", "pnl": 12.5},
        ]
        self.assertAlmostEqual(realized_pnl_for(trades), -37.5, places=2)

    def test_format_daily_nav_line_empty_without_trades_today(self):
        today = date.today().isoformat()
        with patch(
            "notifications.daily_portfolio._history_and_trades",
            return_value=({"trades": []}, [{"timestamp": f"{today}T00:00:00", "type": "BUY"}]),
        ), patch("notifications.daily_portfolio.trades_today", return_value=[]):
            line = format_daily_nav_line(total_value=5000.0)
        self.assertEqual(line, "")

    def test_format_daily_nav_line_with_trades(self):
        today = date.today().isoformat()
        trades = [
            {"timestamp": f"{today}T08:00:00", "type": "SELL", "pnl": -100.0},
            {"timestamp": f"{today}T09:00:00", "type": "BUY"},
        ]
        with patch(
            "notifications.daily_portfolio._history_and_trades",
            return_value=({"trades": trades}, trades),
        ), patch(
            "notifications.daily_portfolio.estimate_nav_at_day_start",
            return_value=5300.0,
        ):
            line = format_daily_nav_line(total_value=4820.0)
        self.assertIn("Heute:", line)
        self.assertIn("1 Käufe / 1 Verkäufe", line)
        self.assertIn("$-480", line)
        self.assertIn("vs. Tagesstart $5,300", line)


if __name__ == "__main__":
    unittest.main()