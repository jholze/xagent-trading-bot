import unittest
from decimal import Decimal
from unittest.mock import patch

from strategies.exit_ladder import (
    advance_ladder_step,
    ladder_enabled,
    resolve_sell_amount,
    resolve_sell_fraction,
)
from strategies.positions import get_key, positions, update_position


class TestExitLadder(unittest.TestCase):
    def setUp(self):
        self.symbol = "LAD/USDT"
        self.tf = "4h"
        self.key = get_key(self.symbol, self.tf)
        self._backup = {k: dict(v) for k, v in positions.items()}
        positions.clear()
        self.params = {
            "strategy_profile": "volatile_altcoin",
            "volatility_tier": "volatile",
            "exit_ladder": {
                "enabled": True,
                "tiers": [0.30, 0.30, 0.20, 0.20],
                "min_tier_notional_usdt": 20,
            },
        }

    def tearDown(self):
        positions.clear()
        positions.update(self._backup)

    def test_ladder_enabled_for_volatile_profile(self):
        self.assertTrue(ladder_enabled(self.params))
        self.assertFalse(ladder_enabled({"strategy_profile": "hermes_baseline"}))

    def test_first_tier_sells_30_percent_of_peak(self):
        update_position(self.symbol, self.tf, "BUY", 1.0, 1000)
        pos = positions[self.key]
        pos["peak_amount"] = 1000.0

        amount = resolve_sell_amount("SELL_30", self.symbol, self.tf, 1.0, self.params)
        self.assertAlmostEqual(amount, 300.0)

    def test_terminal_tier_sells_full_remainder(self):
        update_position(self.symbol, self.tf, "BUY", 1.0, 1000)
        pos = positions[self.key]
        pos["exit_ladder_step"] = 3
        pos["amount"] = Decimal("200")

        amount = resolve_sell_amount("SELL_30", self.symbol, self.tf, 1.0, self.params)
        self.assertAlmostEqual(amount, 200.0)

    def test_stop_full_sells_remainder(self):
        update_position(self.symbol, self.tf, "BUY", 1.0, 1000)
        pos = positions[self.key]
        pos["sold_percent"] = 0.88
        pos["amount"] = Decimal("120")
        pos["peak_amount"] = 1000.0

        frac = resolve_sell_fraction("SELL_STOP_FULL", self.symbol, self.tf, 1.0, self.params)
        self.assertAlmostEqual(frac, 1.0)

    def test_min_remainder_upgrades_to_full(self):
        update_position(self.symbol, self.tf, "BUY", 1.0, 1000)
        pos = positions[self.key]
        pos["exit_ladder_step"] = 3
        pos["amount"] = Decimal("15")
        pos["peak_amount"] = 1000.0

        amount = resolve_sell_amount("SELL_20", self.symbol, self.tf, 1.0, self.params)
        self.assertAlmostEqual(amount, 15.0)

    def test_advance_ladder_step(self):
        pos = {"exit_ladder_step": 1}
        advance_ladder_step(pos, "SELL_30", self.params, amount_sold=100, amount_before=500)
        self.assertEqual(pos["exit_ladder_step"], 2)

    def test_advance_terminal_marks_done(self):
        pos = {"exit_ladder_step": 2}
        advance_ladder_step(pos, "SELL_STOP_FULL", self.params, amount_sold=100, amount_before=100)
        self.assertEqual(pos["exit_ladder_step"], 4)


if __name__ == "__main__":
    unittest.main()