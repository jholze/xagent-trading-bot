"""Phase A: trading modes + grid plan slices + path simulation."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.models import AllocationDecision
from strategies.grid_plan import (
    apply_grid_sell_guards,
    build_grid_plan,
    evaluate_plan_at_price,
    recenter_plan,
    should_block_recenter_below_entry,
    should_recenter,
    simulate_plan_path,
    spacing_atr_mult_for_coin,
)
from strategies.trading_modes import (
    MODE_DEFENSIVE,
    MODE_GRID,
    MODE_HYBRID,
    MODE_MOMENTUM,
    entry_sensor_buy_usdt_frac,
    resolve_trading_mode,
)


class TestTradingModes(unittest.TestCase):
    def test_ranging_high_grid_is_grid_mode(self):
        alloc = AllocationDecision(
            strategy_weights={"grid": 0.6, "momentum": 0.4},
            exposure_multiplier=1.0,
        )
        # pure GRID only when stable/large_cap is known
        self.assertEqual(
            resolve_trading_mode(alloc, volatility_tier="stable"),
            MODE_GRID,
        )
        # unknown tier → HYBRID (safer default)
        self.assertEqual(resolve_trading_mode(alloc), MODE_HYBRID)

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

    def test_volatile_mixed_weights_prefer_hybrid(self):
        """Volatile coins keep momentum path open (entry spikes) → HYBRID."""
        alloc = AllocationDecision(
            strategy_weights={"grid": 0.6, "momentum": 0.4},
            exposure_multiplier=1.0,
        )
        self.assertEqual(
            resolve_trading_mode(alloc, volatility_tier="volatile"),
            MODE_HYBRID,
        )
        self.assertEqual(
            resolve_trading_mode(alloc, volatility_tier="stable"),
            MODE_GRID,
        )

    def test_meme_never_pure_grid(self):
        alloc = AllocationDecision(
            strategy_weights={"grid": 0.7, "momentum": 0.3},
            exposure_multiplier=1.0,
        )
        self.assertEqual(
            resolve_trading_mode(alloc, coin_class="meme", volatility_tier="volatile"),
            MODE_HYBRID,
        )
        self.assertNotEqual(
            resolve_trading_mode(alloc, coin_class="meme"),
            MODE_GRID,
        )

    def test_bar_range_hits_buy_level(self):
        """1h bar wick can hit buy level even if close is above."""
        plan = build_grid_plan("T/USDT", "1h", 100.0, atr_pct=2.0, spacing_atr_mult=1.0)
        buy_lv = min((lv for lv in plan.levels if lv.side == "buy"), key=lambda x: -x.price)
        # close above level, but bar low pierced it
        act = evaluate_plan_at_price(
            plan,
            price=buy_lv.price * 1.01,
            has_position=False,
            bar_low=buy_lv.price * 0.995,
            bar_high=buy_lv.price * 1.02,
        )
        self.assertEqual(act.action, "BUY")

    def test_entry_sensor_slice_by_mode_and_tier(self):
        self.assertLess(
            entry_sensor_buy_usdt_frac(MODE_GRID, volatility_tier="stable"),
            entry_sensor_buy_usdt_frac(MODE_GRID, volatility_tier="volatile"),
        )
        # sensor-entry-guard: MOMENTUM is capped (not full-size)
        self.assertEqual(entry_sensor_buy_usdt_frac(MODE_MOMENTUM), 0.30)


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

    def test_spacing_respects_volatile_stable_split(self):
        base = 0.8
        vol = spacing_atr_mult_for_coin(volatility_tier="volatile", base=base)
        st = spacing_atr_mult_for_coin(volatility_tier="stable", base=base)
        meme = spacing_atr_mult_for_coin(coin_class="meme", base=base)
        large = spacing_atr_mult_for_coin(coin_class="large_cap", base=base)
        self.assertGreater(vol, st)
        self.assertGreaterEqual(meme, vol)
        self.assertLessEqual(large, st)

    def test_volatile_grid_wider_than_stable_on_same_prices(self):
        """Same range series: wider volatile spacing → fewer level fills."""
        import math

        prices = [100 + 5 * math.sin(i / 5.0) for i in range(120)]
        tight = simulate_plan_path(
            prices, spacing_atr_mult=0.55, atr_pct=3.0, base_buy_usdt=400,
        )
        wide = simulate_plan_path(
            prices, spacing_atr_mult=1.25, atr_pct=3.0, base_buy_usdt=400,
        )
        self.assertGreaterEqual(tight["trades"], wide["trades"])

    def test_sell_guard_green_only_blocks_underwater(self):
        plan = build_grid_plan("T/USDT", "1h", 100.0, atr_pct=2.0, spacing_atr_mult=1.0)
        sell_lv = min((lv for lv in plan.levels if lv.side == "sell"), key=lambda x: x.price)
        raw = evaluate_plan_at_price(
            plan, sell_lv.price * 1.001, has_position=True,
        )
        self.assertIn("SELL", raw.action)
        blocked = apply_grid_sell_guards(
            raw,
            plan=plan,
            sell_price=sell_lv.price,
            average_entry=120.0,  # underwater vs entry
            mode="GRID",
            policy={"enabled": True, "green_only_modes": ["GRID"], "green_buffer_pct": 0.15},
        )
        self.assertEqual(blocked.action, "HOLD")
        self.assertIn("blocked", blocked.rationale.lower())

    def test_sell_guard_hybrid_soft_slice(self):
        plan = build_grid_plan("T/USDT", "1h", 100.0, atr_pct=2.0, spacing_atr_mult=1.0)
        sell_lv = min((lv for lv in plan.levels if lv.side == "sell"), key=lambda x: x.price)
        raw = evaluate_plan_at_price(
            plan, sell_lv.price * 1.001, has_position=True,
        )
        soft = apply_grid_sell_guards(
            raw,
            plan=plan,
            sell_price=sell_lv.price,
            average_entry=120.0,
            mode="HYBRID",
            policy={
                "enabled": True,
                "green_only_modes": ["GRID"],
                "soft_underwater_modes": ["HYBRID"],
                "underwater_max_slice": 0.12,
            },
        )
        self.assertIn("SELL", soft.action)
        self.assertLessEqual(soft.sell_pos_frac, 0.12)
        self.assertIn("underwater", soft.rationale.lower())

    def test_block_recenter_below_entry(self):
        self.assertTrue(
            should_block_recenter_below_entry(
                90.0, 100.0,
                policy={"enabled": True, "block_recenter_below_entry": True,
                        "re_center_max_drawdown_pct": 3.0},
            )
        )
        self.assertFalse(
            should_block_recenter_below_entry(
                99.0, 100.0,
                policy={"enabled": True, "block_recenter_below_entry": True,
                        "re_center_max_drawdown_pct": 3.0},
            )
        )


if __name__ == "__main__":
    unittest.main()
