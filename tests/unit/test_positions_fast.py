import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from notifications.daily_portfolio import estimate_nav_at_day_start
from services.gate_balance import fetch_balance_bundle


class TestPositionsFastPath(unittest.TestCase):
    def test_nav_start_cache_avoids_repeat(self):
        import notifications.daily_portfolio as dp

        dp._nav_start_cache.clear()
        snap = {"BTC_USDT_4h": {"amount": 1.0, "peak_amount": 1.0, "average_entry": 1.0}}
        with patch("notifications.daily_portfolio._snapshot_from_orders_before", return_value=snap), \
             patch("notifications.daily_portfolio.compute_sim_cash_from_trades", return_value=1000.0), \
             patch("notifications.daily_portfolio.trades_today", return_value=[{"timestamp": "2026-06-22T10:00:00", "type": "BUY"}]), \
             patch("notifications.daily_portfolio._history_and_trades", return_value=({"trades": []}, [])), \
             patch("notifications.daily_portfolio.get_prices_batch", return_value={"BTC/USDT": 100.0}) as mock_prices:
            first = estimate_nav_at_day_start("paper")
            second = estimate_nav_at_day_start("paper")
        self.assertEqual(first, second)
        self.assertEqual(first, 1100.0)
        mock_prices.assert_called_once()

    def test_balance_bundle_uses_cache(self):
        import services.gate_balance as gb

        gb._balance_cache.clear()
        cfg = MagicMock()
        cfg.trading_mode = "live"
        cfg.raw = {"live": {"dry_run": False}}
        cfg.simulated_balance_usdt = 5000

        adapter = MagicMock()
        exchange = MagicMock()
        exchange.fetch_balance.return_value = {"USDT": {"free": 123.0}, "free": {"USDT": 123.0, "BTC": 0.1}}
        adapter._get_exchange.return_value = exchange

        with patch("services.gate_balance.is_live_dry_run", return_value=False), \
             patch("services.gate_balance.uses_exchange_ledger", return_value=True), \
             patch("services.gate_balance.get_gate_adapter", return_value=adapter):
            first = fetch_balance_bundle(cfg)
            second = fetch_balance_bundle(cfg)
        self.assertEqual(first["usdt"], 123.0)
        self.assertTrue(second["from_cache"])
        exchange.fetch_balance.assert_called_once()


if __name__ == "__main__":
    unittest.main()