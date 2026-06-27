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
        with patch("notifications.telegram_commands.position_display.initial_capital", return_value=5000.0):
            msg = format_portfolio_summary(
                {"virtual_balance": 3952.19, "realized_pnl": -111.82},
                total_unreal=18.5,
                position_count=4,
                cash_balance=3952.19,
                positions_market_value=868.0,
            )
        self.assertIn("Gesamt-PnL", msg)
        self.assertIn("$-93.3", msg)

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

    def test_nav_at_day_start_uses_resolved_ledger_scope(self):
        """Demo scope must not replay positions from empty paper orders."""
        today = date.today().isoformat()
        trades = [
            {"timestamp": f"{today}T08:00:00", "type": "BUY", "mode": "live"},
            {"timestamp": "2026-06-24T10:00:00", "type": "BUY", "mode": "live"},
        ]
        snap = {"CAT_USDT_4h": {"amount": 100.0, "peak_amount": 100.0, "average_entry": 1.0}}
        with patch(
            "notifications.daily_portfolio._history_and_trades",
            return_value=({"trades": trades, "virtual_balance": 9000}, trades),
        ), patch("notifications.daily_portfolio.trades_today", return_value=[trades[0]]), \
             patch("notifications.daily_portfolio.resolve_ledger_scope", return_value="demo"), \
             patch("notifications.daily_portfolio.initial_capital", return_value=10000.0), \
             patch("notifications.daily_portfolio._cash_at_cutoff", return_value=5000.0), \
             patch("notifications.daily_portfolio._snapshot_from_orders_before", return_value=snap) as mock_snap, \
             patch("notifications.daily_portfolio.get_prices_batch", return_value={"CAT/USDT": 2.0}):
            import notifications.daily_portfolio as dp

            dp._nav_start_cache.clear()
            nav = dp.estimate_nav_at_day_start("paper")
        mock_snap.assert_called_once()
        self.assertEqual(mock_snap.call_args[0][1], "demo")
        self.assertAlmostEqual(nav, 5200.0)

    def test_nav_at_day_start_uses_orders_cash_when_trades_incomplete(self):
        """Demo trade_history may only list today's fills; cash must replay orders."""
        today = date.today().isoformat()
        today_trades = [
            {"timestamp": f"{today}T13:50:50", "type": "BUY", "usdt_amount": 1250.0},
        ]
        pre_orders = [
            {
                "status": "filled",
                "side": "buy",
                "timestamps": {"filled": "2026-06-26T10:00:00"},
                "execution": {"usdt": 1000.0},
            }
        ]
        snap = {"CAT_USDT_4h": {"amount": 100.0, "peak_amount": 100.0, "average_entry": 1.0}}
        with patch(
            "notifications.daily_portfolio.trades_today",
            return_value=today_trades,
        ), patch(
            "notifications.daily_portfolio._history_and_trades",
            return_value=({"trades": today_trades, "virtual_balance": 45000}, today_trades),
        ), patch(
            "notifications.daily_portfolio.resolve_ledger_scope",
            return_value="demo",
        ), patch(
            "notifications.daily_portfolio.initial_capital",
            return_value=100000.0,
        ), patch(
            "notifications.daily_portfolio._filled_orders_before",
            return_value=pre_orders,
        ), patch(
            "notifications.daily_portfolio.compute_sim_cash_from_orders",
            return_value=46500.0,
        ) as mock_cash, patch(
            "notifications.daily_portfolio._snapshot_from_orders_before",
            return_value=snap,
        ), patch(
            "notifications.daily_portfolio.get_prices_batch",
            return_value={"CAT/USDT": 2.0},
        ):
            import notifications.daily_portfolio as dp

            dp._nav_start_cache.clear()
            nav = dp.estimate_nav_at_day_start("paper")
        mock_cash.assert_called_once_with(pre_orders, 100000.0)
        self.assertAlmostEqual(nav, 46700.0)

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