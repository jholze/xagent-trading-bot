import unittest

from core.models import MarketContext
from strategies.trailing_stop import compute_trail_pct, evaluate_trailing_stop


class TestTrailingStop(unittest.TestCase):
    def _params(self, **trail):
        base = {
            "strategy_profile": "volatile_altcoin",
            "volatility_tier": "volatile",
            "trailing_stop": {
                "enabled": True,
                "mode": "live",
                "atr_multiplier": 2.0,
                "activation_gain_pct": 10,
                "min_trail_pct": 8,
                "max_trail_pct": 25,
            },
        }
        base["trailing_stop"].update(trail)
        return base

    def test_compute_trail_pct_clamps(self):
        self.assertEqual(compute_trail_pct(2.0, self._params()), 8.0)
        self.assertEqual(compute_trail_pct(20.0, self._params()), 25.0)
        self.assertEqual(compute_trail_pct(10.0, self._params()), 20.0)

    def test_no_trigger_below_activation_gain(self):
        market = MarketContext(
            symbol="H/USDT",
            timeframe="4h",
            current_price=1.05,
            has_position=True,
            average_entry=1.0,
            atr_pct=10.0,
        )
        pos = {"recent_high": 1.10}
        self.assertIsNone(evaluate_trailing_stop(market, pos, self._params()))

    def test_triggers_on_drop_from_recent_high(self):
        market = MarketContext(
            symbol="H/USDT",
            timeframe="4h",
            current_price=0.95,
            has_position=True,
            average_entry=0.85,
            atr_pct=10.0,
        )
        pos = {"recent_high": 1.2}
        cand = evaluate_trailing_stop(market, pos, self._params())
        self.assertIsNotNone(cand)
        self.assertEqual(cand.source, "trailing_stop")
        self.assertIn("Trail", cand.rationale)

    def test_stable_profile_uses_trailing_when_configured(self):
        market = MarketContext(
            symbol="BTC/USDT",
            timeframe="4h",
            current_price=100,
            has_position=True,
            average_entry=90,
            atr_pct=2.0,
        )
        params = self._params()
        params["strategy_profile"] = "hermes_baseline+stable_sell"
        params["volatility_tier"] = "stable"
        params["trailing_stop"]["activation_gain_pct"] = 5
        result = evaluate_trailing_stop(market, {"recent_high": 110}, params)
        self.assertIsNotNone(result)
        self.assertEqual(result.source, "trailing_stop")

    def test_skips_profile_without_trailing_config(self):
        market = MarketContext(
            symbol="BTC/USDT",
            timeframe="4h",
            current_price=100,
            has_position=True,
            average_entry=90,
            atr_pct=2.0,
        )
        params = {"strategy_profile": "hermes_baseline", "volatility_tier": "stable"}
        self.assertIsNone(evaluate_trailing_stop(market, {"recent_high": 110}, params))

    def test_shadow_mode_flag(self):
        market = MarketContext(
            symbol="H/USDT",
            timeframe="4h",
            current_price=0.95,
            has_position=True,
            average_entry=0.85,
            atr_pct=10.0,
        )
        pos = {"recent_high": 1.2}
        cand = evaluate_trailing_stop(market, pos, self._params(mode="shadow"))
        self.assertTrue(cand.shadow_only)


if __name__ == "__main__":
    unittest.main()