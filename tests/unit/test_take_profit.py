import unittest

from strategies.take_profit import mark_triggered_tier, next_trigger_level, tier_key


class TestTakeProfit(unittest.TestCase):
    def test_tier_key(self):
        self.assertEqual(tier_key(40), "tp40")

    def test_next_trigger_level_picks_lowest_undone(self):
        level = next_trigger_level(55.0, [40, 80, 120], {})
        self.assertEqual(level, 40.0)

    def test_next_trigger_level_skips_done(self):
        level = next_trigger_level(90.0, [40, 80, 120], {"tp40": True})
        self.assertEqual(level, 80.0)

    def test_mark_triggered_tier_marks_highest_eligible(self):
        updated = mark_triggered_tier({}, 95.0, [40, 80, 120])
        self.assertTrue(updated["tp80"])
        self.assertNotIn("tp40", updated)

    def test_mark_triggered_tier_legacy_fallback(self):
        updated = mark_triggered_tier({}, 10.0, [])
        self.assertTrue(updated["tp"])


if __name__ == "__main__":
    unittest.main()