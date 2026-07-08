import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from data.cmc_market_cap import (
    entry_sensor_mcap_bounds,
    passes_market_cap_filter,
    reset_market_cap_cache_for_tests,
)


class TestCMCMarketCapFilter(unittest.TestCase):
    def setUp(self):
        reset_market_cap_cache_for_tests()

    def test_min_only_allows_large_cap(self):
        cfg = {"market_cap_min_usd": 5_000_000}
        self.assertEqual(entry_sensor_mcap_bounds(cfg), (5_000_000, None))
        ok, reason = passes_market_cap_filter(8_000_000_000, cfg)
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_min_blocks_micro_cap(self):
        cfg = {"market_cap_min_usd": 5_000_000}
        ok, reason = passes_market_cap_filter(500_000, cfg)
        self.assertFalse(ok)
        self.assertIn("min", reason)

    def test_optional_max_when_explicit(self):
        cfg = {"market_cap_min_usd": 5_000_000, "market_cap_max_usd": 100_000_000}
        self.assertEqual(entry_sensor_mcap_bounds(cfg), (5_000_000, 100_000_000))
        ok, _ = passes_market_cap_filter(200_000_000, cfg)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()