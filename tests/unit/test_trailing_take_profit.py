import unittest
from datetime import datetime, timedelta

from core.actions import SELL_FULL, SELL_PARTIAL_30
from core.models import MarketContext
from strategies.trailing_take_profit import evaluate_trailing_take_profit


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

    def test_skips_when_gain_below_min(self):
        pos = {"recent_high": 1.20, "exit_ladder_step": 0}
        self.assertIsNone(
            evaluate_trailing_take_profit(self._market(current_price=1.05), pos, self._params())
        )


if __name__ == "__main__":
    unittest.main()