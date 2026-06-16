import unittest

from data.lunarcrush_provider import RawLCMetrics
from data.lunarcrush_scorer import LunarCrushSignal, score_lc_metrics


class TestLunarCrushScorer(unittest.TestCase):
    def test_sol_mock_metrics_buy(self):
        metrics = RawLCMetrics("SOL", 74, 62, 45, 120, 76)
        signal = score_lc_metrics(metrics)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.action, "BUY")
        self.assertGreaterEqual(signal.confidence, 55)
        self.assertEqual(signal.source, "lc")

    def test_bearish_metrics_sell(self):
        metrics = RawLCMetrics("XYZ", 38, 55, 500, 400, 32)
        signal = score_lc_metrics(metrics)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.action, "SELL")

    def test_neutral_hold_filtered(self):
        metrics = RawLCMetrics("BTC", 52, 54, 380, 360, 58)
        signal = score_lc_metrics(metrics)
        self.assertIsNone(signal)

    def test_signal_has_lc_fields(self):
        signal = LunarCrushSignal("ARIA", "BUY", 70, galaxy_score=71, alt_rank=88, sentiment=72)
        self.assertEqual(signal.coin, "ARIA")
        self.assertEqual(signal.galaxy_score, 71)
        self.assertEqual(signal.alt_rank, 88)


if __name__ == "__main__":
    unittest.main()