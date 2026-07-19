"""#103 pure coin-fact classifiers (no network)."""

from __future__ import annotations

import unittest

from intelligence.memory.coin_facts import (
    classify_latest_updates_bullet,
    classify_prediction_driver,
    classify_price_analysis_snippet,
    coin_facts_config,
    coin_facts_enabled,
    flags_from_events,
)


class TestClassifyUpdates(unittest.TestCase):
    def test_profit_taking_bullet(self):
        r = classify_latest_updates_bullet(
            "ALLO cools ~10% after AI-token rotation pump; profit-taking noted"
        )
        self.assertIsNotNone(r)
        self.assertEqual(r.event_type, "profit_taking_narrative")
        self.assertLess(r.impact_score, 0)

    def test_unlock_bullet(self):
        r = classify_latest_updates_bullet(
            "Large unlock / low float vesting overhang for ALLO"
        )
        self.assertIsNotNone(r)
        self.assertIn(r.event_type, ("unlock", "supply_overhang"))
        self.assertLess(r.impact_score, 0)

    def test_hard_negative_hack(self):
        r = classify_latest_updates_bullet("Protocol hack drains bridge funds")
        self.assertIsNotNone(r)
        self.assertIn(r.event_type, ("hack", "sec_alert", "exploit"))
        self.assertLessEqual(r.impact_score, -0.8)


class TestClassifyAnalysis(unittest.TestCase):
    def test_flow_only_from_analysis(self):
        r = classify_price_analysis_snippet(
            "No clear secondary driver; move appears flow-driven with volume spike"
        )
        self.assertIsNotNone(r)
        self.assertEqual(r.event_type, "flow_only_move")

    def test_volume_breakout(self):
        r = classify_price_analysis_snippet("Volume surge +174% on breakout")
        self.assertIsNotNone(r)
        self.assertEqual(r.event_type, "volume_breakout")


class TestClassifyPrediction(unittest.TestCase):
    def test_ignore_numeric_price_target(self):
        r = classify_prediction_driver(
            "Price prediction: ALLO will hit $2.50 by 2026",
            section="bullish",
        )
        self.assertIsNotNone(r)
        self.assertEqual(r.event_type, "ignore_target")

    def test_utility_adoption(self):
        r = classify_prediction_driver(
            "Real utility and adoption via Quack AI integration",
            section="bullish",
        )
        self.assertIsNotNone(r)
        self.assertEqual(r.event_type, "utility_adoption")
        self.assertGreater(r.impact_score, 0)


class TestFlagsAndConfig(unittest.TestCase):
    def test_default_disabled(self):
        cfg = coin_facts_config({})
        self.assertFalse(cfg.get("enabled"))
        self.assertFalse(coin_facts_enabled({}))

    def test_flags_hard_neg_beats(self):
        class E:
            def __init__(self, et, imp):
                self.event_type = et
                self.impact_score = imp
                self.description = et

        flags = flags_from_events(
            [E("utility_adoption", 0.3), E("hack", -0.9)]
        )
        self.assertTrue(flags.hard_negative)
        self.assertTrue(flags.utility)
        self.assertLessEqual(flags.min_impact, -0.8)


if __name__ == "__main__":
    unittest.main()
