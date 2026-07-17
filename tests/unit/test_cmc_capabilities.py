import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from data.cmc_capabilities import (
    filter_source_priority,
    probe_capabilities,
    reset_capabilities_cache,
)


class TestCMCCapabilities(unittest.TestCase):
    def tearDown(self):
        reset_capabilities_cache()

    def test_filter_drops_unavailable_endpoints(self):
        caps = {
            "endpoints": {
                "trending/latest": False,
                "trending/gainers-losers": False,
                "listings/latest": True,
            }
        }
        out = filter_source_priority(
            ["trending/latest", "listings/latest"],
            caps,
        )
        self.assertEqual(out, ["listings/latest"])

    def test_filter_fallback_when_all_blocked(self):
        caps = {
            "endpoints": {
                "trending/latest": False,
                "listings/latest": False,
            }
        }
        out = filter_source_priority(["trending/latest"], caps)
        self.assertEqual(out, ["listings/latest"])

    @patch("data.cmc_capabilities._fetch_key_info")
    @patch("data.cmc_capabilities._probe_endpoint")
    def test_probe_caches_builder_listings_only(self, mock_probe, mock_key_info):
        mock_key_info.return_value = {
            "plan_name": "Builder",
            "credits_monthly": 150000,
            "rate_limit_per_min": 300,
        }
        mock_probe.side_effect = lambda _key, eid: eid == "listings/latest"

        caps = probe_capabilities("test-key", force=True)
        self.assertTrue(caps["plan_label"].startswith("Builder"))
        self.assertIn("150,000", caps["plan_label"])
        self.assertTrue(caps["endpoints"]["listings/latest"])
        self.assertFalse(caps["endpoints"]["trending/latest"])

    @patch("data.cmc_capabilities._fetch_key_info")
    @patch("data.cmc_capabilities._probe_endpoint")
    def test_probe_logs_ok_and_blocked(self, mock_probe, mock_key_info):
        mock_key_info.return_value = {"plan_name": "Basic"}
        mock_probe.side_effect = lambda _key, eid: eid in ("listings/latest", "quotes/latest")
        with patch("data.cmc_capabilities.log") as mock_log:
            probe_capabilities("test-key", force=True)
        joined = " ".join(str(c.args[0]) for c in mock_log.call_args_list)
        self.assertIn("listings/latest", joined)
        self.assertIn("blocked=", joined)


if __name__ == "__main__":
    unittest.main()