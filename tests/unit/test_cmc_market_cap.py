import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from data.cmc_market_cap import (
    entry_sensor_mcap_bounds,
    fetch_market_cap_usd,
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

    def test_zero_mcap_response_cached_not_refetched(self):
        """XAUT-style: HTTP 200 but market_cap=0 must not 1Hz retry (CMC dashboard spam)."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": {
                "XAUT": {
                    "symbol": "XAUT",
                    "quote": {"USD": {"price": 2400.0, "market_cap": 0}},
                }
            }
        }
        with patch.dict(os.environ, {"CMC_API_KEY": "test-key"}), \
             patch("data.cmc_market_cap.requests.get", return_value=mock_resp) as mock_get:
            self.assertIsNone(fetch_market_cap_usd("XAUT/USDT"))
            self.assertIsNone(fetch_market_cap_usd("XAUT/USDT"))
            self.assertIsNone(fetch_market_cap_usd("XAUT"))
        self.assertEqual(mock_get.call_count, 1)

    def test_list_response_uses_first_entry(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": {
                "XAUT": [
                    {"symbol": "XAUT", "quote": {"USD": {"market_cap": 1_200_000_000}}},
                    {"symbol": "XAUT", "quote": {"USD": {"market_cap": 1}}},
                ]
            }
        }
        with patch.dict(os.environ, {"CMC_API_KEY": "test-key"}), \
             patch("data.cmc_market_cap.requests.get", return_value=mock_resp):
            self.assertEqual(fetch_market_cap_usd("XAUT"), 1_200_000_000)


if __name__ == "__main__":
    unittest.main()