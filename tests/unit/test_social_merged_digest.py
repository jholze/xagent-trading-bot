import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from services.social_pipeline import SocialPipeline


class TestSocialMergedDigest(unittest.TestCase):
    def setUp(self):
        self.pipeline = SocialPipeline(analyzer=SimpleNamespace(_reload_accounts=lambda: []))

    @patch("notifications.user_explain.explanations_config")
    def test_merged_digest_dedup(self, mock_cfg):
        mock_cfg.return_value = {
            "notify_cmc_digest": True,
            "notify_lc_digest": True,
            "notify_x_digest": True,
            "cmc_digest_min_confidence": 60,
            "lc_digest_min_confidence": 55,
            "x_digest_min_effective_confidence": 70,
        }
        cmc = [SimpleNamespace(coin="BTC", action="BUY", confidence=80)]
        lc = []
        x = []

        self.assertTrue(self.pipeline.should_send_merged_digest(cmc, lc, x))
        self.assertFalse(self.pipeline.should_send_merged_digest(cmc, lc, x))

    @patch("notifications.user_explain.explanations_config")
    def test_x_digest_dedup(self, mock_cfg):
        mock_cfg.return_value = {
            "x_digest_min_effective_confidence": 70,
        }
        x = [
            SimpleNamespace(
                coin="SOL", action="BUY", confidence=80, effective_confidence=80, post_id="p1"
            )
        ]
        self.assertTrue(self.pipeline.should_send_x_digest(x))
        self.assertFalse(self.pipeline.should_send_x_digest(x))


if __name__ == "__main__":
    unittest.main()