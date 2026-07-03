import unittest
from datetime import datetime, timedelta

from core.actions import SELL_FULL
from core.models import MarketContext
from strategies.profit_max_lifetime import evaluate_profit_max_lifetime


class TestProfitMaxLifetime(unittest.TestCase):
    def _params(self, **overrides):
        base = {
            "strategy_profile": "volatile_altcoin",
            "profit_max_lifetime": {
                "enabled": True,
                "mode": "live",
                "arm_gain_pct": 3.0,
                "max_hours": 96,
                "min_gain_pct": 1.0,
                "skip_if_peak_above_pct": 40.0,
            },
        }
        base["profit_max_lifetime"].update(overrides)
        return base

    def _market(self, **kwargs):
        defaults = dict(
            symbol="H/USDT",
            timeframe="4h",
            current_price=1.05,
            has_position=True,
            average_entry=1.0,
            atr_pct=8.0,
        )
        defaults.update(kwargs)
        return MarketContext(**defaults)

    def test_no_trigger_before_armed(self):
        now = datetime(2026, 7, 5, 12, 0, 0)
        pos = {"profit_armed_at": None, "recent_high": 1.02}
        self.assertIsNone(
            evaluate_profit_max_lifetime(self._market(), pos, self._params(), now=now)
        )

    def test_triggers_after_max_hours_in_profit(self):
        now = datetime(2026, 7, 5, 12, 0, 0)
        armed = (now - timedelta(hours=100)).isoformat()
        pos = {"profit_armed_at": armed, "recent_high": 1.08, "profit_max_lifetime_done": False}
        cand = evaluate_profit_max_lifetime(self._market(), pos, self._params(), now=now)
        self.assertIsNotNone(cand)
        self.assertEqual(cand.action, SELL_FULL)
        self.assertEqual(cand.source, "profit_max_lifetime")

    def test_skips_runners_above_peak_threshold(self):
        now = datetime(2026, 7, 5, 12, 0, 0)
        armed = (now - timedelta(hours=100)).isoformat()
        pos = {"profit_armed_at": armed, "recent_high": 1.50}
        self.assertIsNone(
            evaluate_profit_max_lifetime(self._market(), pos, self._params(), now=now)
        )

    def test_skips_when_not_in_profit(self):
        now = datetime(2026, 7, 5, 12, 0, 0)
        armed = (now - timedelta(hours=100)).isoformat()
        pos = {"profit_armed_at": armed, "recent_high": 1.02}
        self.assertIsNone(
            evaluate_profit_max_lifetime(self._market(current_price=0.99), pos, self._params(), now=now)
        )


if __name__ == "__main__":
    unittest.main()