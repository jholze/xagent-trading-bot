"""Dynamic social chorus — CMC / Santiment / LunarCrush, only when they agree."""

from __future__ import annotations

import unittest

from intelligence.regime_detector import RegimeDetector
from intelligence.social_dynamic_weight import evaluate_social_chorus


def _cfg(**overrides):
    base = {"enabled": True}
    base.update(overrides)
    return base


class TestSocialChorus(unittest.TestCase):
    def test_disabled_no_boost(self):
        c = evaluate_social_chorus(
            {"cmc_action": "BUY", "cmc_confidence": 88, "fusion_regime": "RISK_ON"},
            cfg=_cfg(enabled=False),
        )
        self.assertFalse(c.boost_buys)
        self.assertEqual(c.sentiment_weight, 0.38)

    def test_thin_one_source(self):
        c = evaluate_social_chorus(
            {"cmc_action": "BUY", "cmc_confidence": 88, "cmc_quotes_fallback": True},
            cfg=_cfg(),
        )
        self.assertEqual(c.agree, "thin")
        self.assertFalse(c.boost_buys)

    def test_cmc_quotes_plus_santiment_risk_on_boosts(self):
        """Live tape: CMC derived BUY 88% + Oracle/Santiment RISK_ON → chorus."""
        c = evaluate_social_chorus(
            {
                "cmc_action": "BUY",
                "cmc_confidence": 88,
                "cmc_quotes_fallback": True,
                "fusion_regime": "RISK_ON",
                "santiment_sentiment": 0.55,
            },
            cfg=_cfg(),
        )
        self.assertEqual(c.agree, "bull")
        self.assertTrue(c.boost_buys)
        self.assertGreaterEqual(c.sentiment_weight, 0.50)
        self.assertIn("cmc", c.sources)
        self.assertIn("santiment", c.sources)
        # 55 trust * 1.25 = 68.75 → 88 * 0.6875 ≈ 60.5 clears min_confidence 55
        self.assertGreaterEqual(88.0 * (55.0 * c.cmc_trust_mult / 100.0), 55.0)

    def test_weak_quotes_do_not_count(self):
        c = evaluate_social_chorus(
            {
                "cmc_action": "BUY",
                "cmc_confidence": 62,
                "cmc_quotes_fallback": True,
                "fusion_regime": "RISK_ON",
            },
            cfg=_cfg(),
        )
        self.assertEqual(c.agree, "thin")

    def test_fusion_risk_off_never_boosts(self):
        c = evaluate_social_chorus(
            {
                "cmc_action": "BUY",
                "cmc_confidence": 90,
                "cmc_quotes_fallback": True,
                "fusion_regime": "RISK_OFF",
                "santiment_sentiment": -0.45,
            },
            cfg=_cfg(),
        )
        self.assertFalse(c.boost_buys)
        self.assertIn("fusion_risk_off", c.reasons)

    def test_harvest_raises_weight_but_not_buy_boost(self):
        c = evaluate_social_chorus(
            {
                "cmc_action": "BUY",
                "cmc_confidence": 88,
                "cmc_quotes_fallback": True,
                "fusion_regime": "RISK_ON",
                "climax_mode": "harvest",
            },
            cfg=_cfg(),
            climax_mode="harvest",
        )
        self.assertEqual(c.agree, "bull")
        self.assertFalse(c.boost_buys)
        self.assertEqual(c.cmc_trust_mult, 1.0)

    def test_mixed_cmc_buy_lunar_sell(self):
        c = evaluate_social_chorus(
            {
                "cmc_action": "BUY",
                "cmc_confidence": 80,
                "lc_action": "SELL",
                "lunarcrush_sentiment": 30,
                "fusion_regime": "NEUTRAL",
            },
            cfg=_cfg(),
        )
        self.assertEqual(c.agree, "mixed")
        self.assertFalse(c.boost_buys)
        self.assertEqual(c.sentiment_weight, 0.38)

    def test_three_source_bull_includes_lunar(self):
        c = evaluate_social_chorus(
            {
                "cmc_action": "BUY",
                "cmc_confidence": 70,
                "lc_action": "BUY",
                "lunarcrush_sentiment": 70,
                "fusion_regime": "RISK_ON",
            },
            cfg=_cfg(),
        )
        self.assertEqual(c.n_bull, 3)
        self.assertTrue(c.boost_buys)
        self.assertEqual(c.sources, ("cmc", "santiment", "lunar"))

    def test_bear_chorus_no_buy_boost(self):
        c = evaluate_social_chorus(
            {
                "cmc_action": "SELL",
                "fusion_regime": "RISK_OFF",
                "lc_action": "SELL",
            },
            cfg=_cfg(block_on_fusion_risk_off=False),
        )
        self.assertEqual(c.agree, "bear")
        self.assertFalse(c.boost_buys)
        self.assertGreater(c.sentiment_weight, 0.38)


class TestRegimeUsesChorusWeights(unittest.TestCase):
    def test_detector_details_include_chorus(self):
        det = RegimeDetector(
            {
                "tech_weight": 0.62,
                "sentiment_weight": 0.38,
                "dynamic_social": {"enabled": True, "bull_sentiment_weight": 0.55},
            }
        )
        import pandas as pd

        prices = [100.0 + i * 0.05 for i in range(80)]
        df = pd.DataFrame(
            {"close": prices, "high": [p + 1 for p in prices], "low": [p - 1 for p in prices]}
        )
        result = det.detect(
            {"symbol": "PEOPLE/USDT", "timeframe": "4h"},
            df,
            current_price=104.0,
            atr_pct=4.0,
            social_context={
                "cmc_action": "BUY",
                "cmc_confidence": 88,
                "cmc_quotes_fallback": True,
                "fusion_regime": "RISK_ON",
                "santiment_sentiment": 0.55,
            },
        )
        self.assertIn(result.details.get("social_chorus"), ("bull", "thin", "mixed", "bear"))
        if result.details.get("social_chorus") == "bull":
            self.assertGreaterEqual(float(result.details["sentiment_weight"]), 0.50)


if __name__ == "__main__":
    unittest.main()
