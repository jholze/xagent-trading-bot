"""Phase A: trading modes + grid plan slices + path simulation."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.models import AllocationDecision
from strategies.grid_plan import (
    build_grid_plan,
    evaluate_plan_at_price,
    recenter_plan,
    should_recenter,
    simulate_plan_path,
)
from strategies.trading_modes import (
    MODE_DEFENSIVE,
    MODE_GRID,
    MODE_HYBRID,
    MODE_MOMENTUM,
    resolve_trading_mode,
)


class TestTradingModes(unittest.TestCase):
    def test_ranging_high_grid_is_grid_mode(self):
        alloc = AllocationDecision(
            strategy_weights={"grid": 0.6, "momentum": 0.4},
            exposure_multiplier=1.0,
        )
        self.assertEqual(resolve_trading_mode(alloc), MODE_GRID)

    def test_defensive(self):
        alloc = AllocationDecision(
            strategy_weights={"grid": 0.0, "momentum": 0.3},
            exposure_multiplier=0.3,
            defensive_mode=True,
        )
        self.assertEqual(resolve_trading_mode(alloc), MODE_DEFENSIVE)

    def test_momentum(self):
        alloc = AllocationDecision(
            strategy_weights={"grid": 0.15, "momentum": 0.85},
            exposure_multiplier=1.0,
        )
        self.assertEqual(resolve_trading_mode(alloc), MODE_MOMENTUM)

    def test_hybrid_balanced(self):
        alloc = AllocationDecision(
            strategy_weights={"grid": 0.4, "momentum": 0.4},
            exposure_multiplier=0.7,
        )
        self.assertEqual(resolve_trading_mode(alloc), MODE_HYBRID)

    def test_force_grid(self):
        self.assertEqual(resolve_trading_mode(None, force_grid=True), MODE_GRID)


class TestGridPlan(unittest.TestCase):
    def test_build_levels(self):
        plan = build_grid_plan("T/USDT", "4h", 100.0, atr_pct=2.0, n_buy_levels=3, n_sell_levels=3)
        self.assertEqual(len(plan.levels), 6)
        buys = [lv for lv in plan.levels if lv.side == "buy"]
        sells = [lv for lv in plan.levels if lv.side == "sell"]
        self.assertEqual(len(buys), 3)
        self.assertTrue(all(lv.price < 100 for lv in buys))
        self.assertTrue(all(lv.price > 100 for lv in sells))

    def test_buy_touch_marks_filled(self):
        plan = build_grid_plan("T/USDT", "4h", 100.0, atr_pct=2.0, spacing_atr_mult=1.0)
        buy_lv = min((lv for lv in plan.levels if lv.side == "buy"), key=lambda x: abs(x.price - 100))
        act = evaluate_plan_at_price(plan, buy_lv.price * 0.999, has_position=False)
        self.assertEqual(act.action, "BUY")
        self.assertGreater(act.buy_usdt_frac, 0)
        self.assertTrue(any(lv.filled for lv in plan.levels if lv.side == "buy"))

    def test_sell_requires_position(self):
        plan = build_grid_plan("T/USDT", "4h", 100.0, atr_pct=2.0, spacing_atr_mult=1.0)
        sell_lv = min((lv for lv in plan.levels if lv.side == "sell"), key=lambda x: abs(x.price - 100))
        act = evaluate_plan_at_price(plan, sell_lv.price * 1.001, has_position=False)
        self.assertEqual(act.action, "HOLD")
        act2 = evaluate_plan_at_price(plan, sell_lv.price * 1.001, has_position=True)
        self.assertIn("SELL", act2.action)
        self.assertGreater(act2.sell_pos_frac, 0)

    def test_recenter(self):
        plan = build_grid_plan("T/USDT", "4h", 100.0, atr_pct=2.0, spacing_atr_mult=0.5)
        self.assertTrue(should_recenter(plan, 120.0, atr_pct=2.0, re_center_atr_mult=2.0))
        plan2 = recenter_plan(plan, 120.0, atr_pct=2.0, spacing_atr_mult=0.5)
        self.assertAlmostEqual(plan2.center, 120.0, places=4)

    def test_ranging_simulation_has_rotation(self):
        import math

        prices = [100 + 6 * math.sin(i / 6.0) for i in range(180)]
        res = simulate_plan_path(prices, initial_cash=10_000, base_buy_usdt=400)
        self.assertGreaterEqual(res["trades"], 2)
        self.assertIn("final_equity", res)
        self.assertGreater(res["final_equity"], 0)


if __name__ == "__main__":
    unittest.main()
