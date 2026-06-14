import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from scripts.x_health_check import _x_posts_stats, run_checks


class TestXHealthCheck(unittest.TestCase):
    @patch("scripts.x_health_check.load_x_posts")
    def test_x_posts_stats_counts_real_ids(self, mock_load):
        mock_load.return_value = {
            "posts": [
                {"post_id": "mock_1", "account": "A"},
                {"post_id": "1234567890123456789", "account": "B", "parsed_action": "BUY", "coin": "SOL"},
            ]
        }
        stats = _x_posts_stats()
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["real_ids"], 1)
        self.assertEqual(stats["latest_account"], "B")

    @patch("scripts.x_health_check._check_api_token", return_value=(True, "OK"))
    @patch("scripts.x_health_check.XApiV2Provider")
    @patch("scripts.x_health_check.get_x_provider")
    @patch("scripts.x_health_check.get_config")
    @patch("scripts.x_health_check.load_x_accounts")
    @patch("scripts.x_health_check.load_x_posts")
    @patch("scripts.x_health_check._sandbox_count", return_value=0)
    def test_run_checks_ok_with_live_provider(
        self,
        _sandbox,
        mock_posts,
        mock_accounts,
        mock_cfg,
        mock_provider,
        mock_api_cls,
        _api_check,
    ):
        mock_cfg.return_value = {"use_mock_x_data": False, "use_grok_x_search": False}

        class P:
            __name__ = "XApiV2Provider"

        mock_provider.return_value = P()
        mock_accounts.return_value = [{"handle": "Test", "enabled": True}]
        mock_posts.return_value = {
            "posts": [{"post_id": "999", "account": "Test", "parsed_action": "HOLD", "coin": "BTC"}]
        }
        mock_api_cls.return_value.fetch_new_posts.return_value = []

        lines, ok = run_checks()
        self.assertTrue(ok)
        self.assertTrue(any("X Health Check" in line for line in lines))


if __name__ == "__main__":
    unittest.main()