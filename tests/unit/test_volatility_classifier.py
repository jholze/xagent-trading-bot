import unittest

from intelligence.volatility_classifier import tier_score, volatility_tier


_TIER_SCORING_CFG = {
    "tier_scoring": {
        "enabled": True,
        "volatile_enter_score": 4,
        "volatile_exit_score": 2,
        "atr_bands": [
            {"min_pct": 10.0, "points": 4},
            {"min_pct": 6.0, "points": 3},
            {"min_pct": 5.0, "points": 2},
            {"min_pct": 4.0, "points": 1},
        ],
        "class_points": {"meme": 2, "mid_cap": 0, "large_cap": -99},
        "source_points": {"dry_run_expansion": 1, "cmc_trending": 1},
        "range_24h_min_pct": 12.0,
        "range_24h_points": 1,
        "change_24h_down_pct": -5.0,
        "change_24h_down_points": 1,
    },
}


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

    def test_lab_like_volatile_with_atr_and_source(self):
        coin = {"symbol": "LAB/USDT", "source": "dry_run_expansion"}
        cfg = dict(_TIER_SCORING_CFG)
        self.assertEqual(
            volatility_tier(coin, 6.5, cfg, range_24h_pct=18.0, change_24h_pct=-8.0),
            "volatile",
        )
        self.assertGreaterEqual(tier_score(coin, 6.5, cfg, range_24h_pct=18.0, change_24h_pct=-8.0), 4)

    def test_borderline_stable_without_range(self):
        coin = {"symbol": "TRX/USDT", "source": "dry_run_expansion"}
        cfg = dict(_TIER_SCORING_CFG)
        self.assertEqual(volatility_tier(coin, 4.0, cfg), "stable")
        self.assertEqual(tier_score(coin, 4.0, cfg), 2)

    def test_borderline_volatile_with_high_range_and_trend(self):
        coin = {"symbol": "TRX/USDT", "source": "dry_run_expansion"}
        cfg = dict(_TIER_SCORING_CFG)
        self.assertEqual(
            volatility_tier(coin, 4.5, cfg, range_24h_pct=15.0, change_24h_pct=-6.0),
            "volatile",
        )

    def test_hysteresis_stays_volatile_in_gray_zone(self):
        coin = {"symbol": "TRX/USDT", "source": "dry_run_expansion", "_volatility_tier_prev": "volatile"}
        cfg = dict(_TIER_SCORING_CFG)
        self.assertEqual(volatility_tier(coin, 4.0, cfg), "volatile")

    def test_hysteresis_exits_volatile_when_score_drops(self):
        coin = {"symbol": "TRX/USDT", "_volatility_tier_prev": "volatile"}
        cfg = dict(_TIER_SCORING_CFG)
        self.assertEqual(volatility_tier(coin, 2.0, cfg), "stable")

    def test_legacy_atr_band_when_scoring_disabled(self):
        coin = {"symbol": "MID/USDT"}
        cfg = {
            "atr_volatile_enter_pct": 5.0,
            "atr_stable_exit_pct": 3.5,
            "tier_scoring": {"enabled": False},
        }
        self.assertEqual(volatility_tier(coin, 4.2, cfg), "stable")
        coin["_volatility_tier_prev"] = "volatile"
        self.assertEqual(volatility_tier(coin, 4.2, cfg), "volatile")