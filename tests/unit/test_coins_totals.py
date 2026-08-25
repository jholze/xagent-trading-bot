import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from notifications.telegram_commands.position_display import aggregate_open_coins_totals


class TestCoinsTotals(unittest.TestCase):
    def test_marktwert_minus_einstand_equals_unreal(self):
        active = [
            {"symbol": "BTC/USDT", "amount": 1, "average_entry": 90_000},
            {"symbol": "ARIA/USDT", "amount": 100, "average_entry": 0.04},
        ]
        prices = {"BTC/USDT": 95_000.0, "ARIA/USDT": 0.05}
        totals = aggregate_open_coins_totals(active, prices)
        self.assertAlmostEqual(totals["cost_basis"], 90_004.0, places=0)
        self.assertAlmostEqual(totals["marktwert"], 95_005.0, places=0)
        self.assertAlmostEqual(
            totals["unreal"],
            totals["marktwert"] - totals["cost_basis"],
            places=0,
        )

    def test_missing_price_keeps_einstand_but_zero_marktwert(self):
        active = [{"symbol": "CAT/USDT", "amount": 100, "average_entry": 0.01}]
        totals = aggregate_open_coins_totals(active, {"CAT/USDT": 0.0})
        self.assertAlmostEqual(totals["cost_basis"], 1.0, places=2)
        self.assertEqual(totals["marktwert"], 0.0)
        self.assertEqual(totals["missing_prices"], 1)

    def test_short_einstand_is_margin_not_notional(self):
        active = [{
            "symbol": "H/USDT",
            "amount": 100,
            "average_entry": 2.0,
            "side": "short",
            "leverage": 2,
        }]
        prices = {"H/USDT": 1.8}
        totals = aggregate_open_coins_totals(active, prices)
        self.assertAlmostEqual(totals["cost_basis"], 100.0, places=2)
        self.assertAlmostEqual(totals["unreal"], 20.0, places=2)
        self.assertAlmostEqual(totals["marktwert"], 120.0, places=2)


if __name__ == "__main__":
    unittest.main()