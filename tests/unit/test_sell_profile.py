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

    def test_stable_overlay_applies_bb_sell_min_gain(self):
        """Stable structure sells must receive bb_sell_min_gain_pct (TAO/WLD fix)."""
        base = {"strategy_profile": "hermes_baseline"}
        stable_cfg = {
            "enabled": True,
            "bb_sell_enabled": True,
            "bb_sell_min_gain_pct": 2,
            "bb_sell_rsi_min": 62,
            "bb_sell_upper_ratio": 0.99,
            "bb_sell_requires_ta": False,
        }
        result = apply_position_sell_overlay(
            base,
            tier="stable",
            has_position=True,
            symbol="WLD/USDT",
            tf="1h",
            volatile_cfg={},
            stable_cfg=stable_cfg,
        )
        self.assertEqual(result.get("bb_sell_min_gain_pct"), 2)
        self.assertTrue(result.get("bb_sell_enabled"))
        self.assertEqual(result.get("bb_sell_rsi_min"), 62)

        # End-to-end: structure path respects overlaid floor
        from core.models import MarketContext
        from strategies.market_structure import evaluate_market_structure_sells

        entry, px = 0.3227, 0.3229  # ~+0.06%
        market = MarketContext(
            symbol="WLD/USDT",
            timeframe="1h",
            current_price=px,
            rsi=77.0,
            lower_bb=0.3,
            middle_bb=0.31,
            upper_bb=0.3158,
            atr_pct=3.0,
            vol_multiplier=1.0,
            has_position=True,
            average_entry=entry,
        )
        cands = evaluate_market_structure_sells(
            market,
            result,
            {"rsi_sell_tiers_done": {}, "recent_high": px},
        )
        self.assertEqual(cands, [])

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