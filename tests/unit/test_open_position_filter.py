import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from strategies.positions import is_open_position, list_active_positions, positions, update_position
from decimal import Decimal


class TestOpenPositionFilter(unittest.TestCase):
    def setUp(self):
        self._backup = {
            k: {**v, "amount": Decimal(str(v["amount"]))} for k, v in positions.items()
        }
        positions.clear()

    def tearDown(self):
        positions.clear()
        positions.update(self._backup)

    def test_btc_fractional_amount_counts_as_open(self):
        update_position("BTC/USDT", "4h", "BUY", 65723.0, 0.004441367557780381)
        active = list_active_positions()
        symbols = [p["symbol"] for p in active]
        self.assertIn("BTC/USDT", symbols)

    def test_dust_below_one_usdt_not_open(self):
        pos = {"amount": 0.0001, "average_entry": 0.000001}
        self.assertFalse(is_open_position(pos))

    def test_missing_entry_still_open_with_material_amount(self):
        pos = {"amount": 0.004, "average_entry": 0, "last_buy_price": 0}
        self.assertTrue(is_open_position(pos))


if __name__ == "__main__":
    unittest.main()