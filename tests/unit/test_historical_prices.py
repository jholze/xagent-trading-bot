import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from historical_prices import check_target_hit, get_return_pct


class TestHistoricalPrices(unittest.TestCase):
    def test_check_target_hit_buy(self):
        self.assertTrue(check_target_hit("BUY", 100, 105, 106, 99))
        self.assertFalse(check_target_hit("BUY", 100, 105, 104, 99))

    def test_check_target_hit_sell(self):
        self.assertTrue(check_target_hit("SELL", 100, 95, 101, 94))
        self.assertFalse(check_target_hit("SELL", 100, 95, 101, 96))

    def test_get_return_pct(self):
        self.assertAlmostEqual(get_return_pct(100, 103), 3.0)


if __name__ == "__main__":
    unittest.main()