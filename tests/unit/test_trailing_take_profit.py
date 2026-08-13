import unittest
from datetime import datetime, timedelta

from core.actions import SELL_FULL, SELL_PARTIAL_30
from core.models import MarketContext
from strategies.trailing_take_profit import (
    evaluate_trailing_take_profit,
    resolve_trail_pct,
)


class TestTrailingTakeProfit(unittest.TestCase):
    def _params(self, **overrides):
        base = {
            "strategy_profile": "volatile_altcoin",
            "exit_ladder": {
                "enabled": True,
                "tiers": [0.6, 0.3, 0.1],
                "min_remainder_pct": 0.05,
                "min_remainder_usdt_floor": 200,
            },
            "trailing_take_profit": {
                "enabled": True,
                "mode": "live",
                "trail_pct": 6.0,
                "arm_gain_pct": 15.0,
                "min_gain_pct": 10.0,
                "max_steps": 3,
                "cooldown_hours": 6,
            },
        }
        base["trailing_take_profit"].update(overrides)
        return base

    def _market(self, **kwargs):
        defaults = dict(
            symbol="H/USDT",
            timeframe="4h",
            current_price=1.12,
            has_position=True,
            average_entry=1.0,
            atr_pct=8.0,
        )
        defaults.update(kwargs)
        return MarketContext(**defaults)

    def test_no_trigger_before_arm_gain(self):
        pos = {"recent_high": 1.14, "exit_ladder_step": 0}
        self.assertIsNone(
            evaluate_trailing_take_profit(self._market(current_price=1.12), pos, self._params())
        )

    def test_triggers_partial_via_exit_ladder(self):
        pos = {"recent_high": 1.20, "exit_ladder_step": 0, "peak_amount": 100.0, "amount": 100.0}
        cand = evaluate_trailing_take_profit(self._market(current_price=1.12), pos, self._params())
        self.assertIsNotNone(cand)
        self.assertEqual(cand.action, SELL_PARTIAL_30)
        self.assertEqual(cand.source, "trailing_take_profit")

    def test_respects_cooldown(self):
        now = datetime(2026, 7, 1, 12, 0, 0)
        pos = {
            "recent_high": 1.20,
            "exit_ladder_step": 1,
            "last_trail_tp_at": (now - timedelta(hours=2)).isoformat(),
        }
        self.assertIsNone(
            evaluate_trailing_take_profit(
                self._market(current_price=1.12), pos, self._params(), now=now,
            )
        )

    def test_last_ladder_step_sells_full(self):
        pos = {"recent_high": 1.20, "exit_ladder_step": 2}
        cand = evaluate_trailing_take_profit(self._market(current_price=1.12), pos, self._params())
        self.assertEqual(cand.action, SELL_FULL)

    def test_skips_when_gain_below_min_if_soft_trail_disabled(self):
        pos = {"recent_high": 1.20, "exit_ladder_step": 0}
        params = self._params()
        params["trailing_take_profit"]["trail_above_zero_after_arm"] = False
        self.assertIsNone(
            evaluate_trailing_take_profit(
                self._market(current_price=1.05), pos, params
            )
        )

    def test_allows_green_trail_below_min_after_peak_arm(self):
        """Peak +20%, gain +5%, drop from high large → still take profit (not wait min_gain)."""
        pos = {"recent_high": 1.20, "exit_ladder_step": 0}
        cand = evaluate_trailing_take_profit(
            self._market(current_price=1.05), pos, self._params()
        )
        self.assertIsNotNone(cand)
        self.assertEqual(cand.source, "trailing_take_profit")
        self.assertGreaterEqual(cand.priority, 6)

    def test_completed_ladder_returns_full_close(self):
        pos = {"recent_high": 1.20, "exit_ladder_step": 3}
        cand = evaluate_trailing_take_profit(self._market(current_price=1.12), pos, self._params())
        self.assertIsNotNone(cand)
        self.assertEqual(cand.action, SELL_FULL)

    def test_resolve_trail_pct_scales_with_peak(self):
        cfg = {
            "dynamic_trail": True,
            "trail_pct_min": 3.0,
            "trail_pct_max": 12.0,
            "trail_pct_scale_start_pct": 18.0,
            "trail_pct_scale_peak_pct": 45.0,
        }
        self.assertEqual(resolve_trail_pct(13.0, cfg), 3.0)
        self.assertEqual(resolve_trail_pct(45.0, cfg), 12.0)
        self.assertGreater(resolve_trail_pct(30.0, cfg), 6.0)
        self.assertLess(resolve_trail_pct(30.0, cfg), 12.0)

    def test_full_close_gain_prefers_sell_full_without_trail_drop(self):
        """Once armed past full_close_gain_pct, close fully instead of waiting to trail."""
        pos = {"recent_high": 1.13, "exit_ladder_step": 0, "peak_amount": 100.0, "amount": 100.0}
        params = self._params(
            dynamic_trail=False,
            trail_pct=6.0,
            arm_gain_pct=10.0,
            min_gain_pct=8.0,
            full_close_gain_pct=12.0,
        )
        params["exit_ladder"]["enabled"] = False
        cand = evaluate_trailing_take_profit(
            self._market(current_price=1.13), pos, params,
        )
        self.assertIsNotNone(cand)
        self.assertEqual(cand.action, SELL_FULL)
        self.assertIn("full_close", cand.rationale)

    def test_dynamic_trail_triggers_on_modest_peak_pullback(self):
        """Peak +13%, current +9.5%: tight trail should fire (closes exit_sensor gap)."""
        pos = {"recent_high": 1.13, "exit_ladder_step": 0, "peak_amount": 100.0, "amount": 100.0}
        params = self._params(
            dynamic_trail=True,
            trail_pct_min=3.0,
            trail_pct_max=12.0,
            trail_pct_scale_peak_pct=45.0,
            arm_gain_pct=12.0,
            min_gain_pct_floor=8.0,
        )
        cand = evaluate_trailing_take_profit(
            self._market(current_price=1.095), pos, params,
        )
        self.assertIsNotNone(cand)
        self.assertIn("trail 3.0%", cand.rationale)

    def test_dynamic_trail_allows_runner_pullback(self):
        """Peak +50%, -5% pullback: wide trail should hold."""
        pos = {"recent_high": 1.50, "exit_ladder_step": 0, "peak_amount": 100.0, "amount": 100.0}
        params = self._params(
            dynamic_trail=True,
            trail_pct_min=3.0,
            trail_pct_max=12.0,
            trail_pct_scale_peak_pct=45.0,
            arm_gain_pct=12.0,
            min_gain_pct_floor=8.0,
        )
        self.assertIsNone(
            evaluate_trailing_take_profit(
                self._market(current_price=1.425), pos, params,
            )
        )

    def test_fixed_trail_when_dynamic_disabled(self):
        pos = {"recent_high": 1.13, "exit_ladder_step": 0, "peak_amount": 100.0, "amount": 100.0}
        params = self._params(
            dynamic_trail=False,
            trail_pct=6.0,
            arm_gain_pct=12.0,
        )
        self.assertIsNone(
            evaluate_trailing_take_profit(
                self._market(current_price=1.095), pos, params,
            )
        )


if __name__ == "__main__":
    unittest.main()