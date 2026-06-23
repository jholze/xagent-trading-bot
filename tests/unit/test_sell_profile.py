import unittest

from strategies.sell_profile import apply_position_sell_overlay


class TestSellProfile(unittest.TestCase):
    def test_volatile_overlay_applies_aggressive_ladder(self):
        base = {"strategy_profile": "hermes_baseline", "rsi_sell_30": 70}
        volatile_cfg = {
            "enabled": True,
            "rsi_sell_30": 62,
            "exit_ladder": {"enabled": True, "tiers": [0.6, 0.3, 0.1]},
        }
        result = apply_position_sell_overlay(
            base,
            tier="volatile",
            has_position=True,
            symbol="H/USDT",
            tf="4h",
            volatile_cfg=volatile_cfg,
            stable_cfg={},
        )
        self.assertEqual(result["strategy_profile"], "hermes_baseline+volatile")
        self.assertEqual(result["rsi_sell_30"], 62)
        self.assertEqual(result["exit_ladder"]["tiers"], [0.6, 0.3, 0.1])

    def test_stable_overlay_keeps_conservative_ladder(self):
        base = {"strategy_profile": "hermes_baseline"}
        stable_cfg = {
            "enabled": True,
            "exit_ladder": {"enabled": True, "tiers": [0.3, 0.3, 0.2, 0.2]},
            "take_profit_tiers": [60, 100, 150],
        }
        result = apply_position_sell_overlay(
            base,
            tier="stable",
            has_position=True,
            symbol="BTC/USDT",
            tf="4h",
            volatile_cfg={},
            stable_cfg=stable_cfg,
        )
        self.assertEqual(result["strategy_profile"], "hermes_baseline+stable_sell")
        self.assertEqual(result["take_profit_tiers"], [60, 100, 150])

    def test_no_overlay_without_position(self):
        base = {"strategy_profile": "hermes_baseline"}
        result = apply_position_sell_overlay(
            base,
            tier="volatile",
            has_position=False,
            symbol="H/USDT",
            tf="4h",
            volatile_cfg={"rsi_sell_30": 62},
            stable_cfg={},
        )
        self.assertEqual(result, base)


if __name__ == "__main__":
    unittest.main()