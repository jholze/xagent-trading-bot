import unittest

from intelligence.volatility_classifier import volatility_tier


class TestVolatilityClassifier(unittest.TestCase):
    def test_eth_is_stable(self):
        coin = {"symbol": "ETH/USDT"}
        self.assertEqual(volatility_tier(coin, 1.7, {}), "stable")

    def test_high_atr_is_volatile(self):
        coin = {"symbol": "H/USDT"}
        self.assertEqual(volatility_tier(coin, 49.0, {"atr_volatile_enter_pct": 5.0}), "volatile")

    def test_frozen_tier_wins(self):
        coin = {"symbol": "H/USDT"}
        self.assertEqual(volatility_tier(coin, 49.0, {}, frozen_tier="stable"), "stable")

    def test_micro_cap_override(self):
        coin = {"symbol": "CAT/USDT", "market_cap_tier": "micro"}
        self.assertEqual(volatility_tier(coin, 2.0, {"micro_cap_override": True}), "volatile")

    def test_low_atr_mid_cap_stable(self):
        coin = {"symbol": "CAT/USDT"}
        self.assertEqual(
            volatility_tier(coin, 2.0, {"atr_volatile_enter_pct": 5.0, "atr_stable_exit_pct": 3.5}),
            "stable",
        )