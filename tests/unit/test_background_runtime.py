import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import services.background_runtime as bg


class TestBackgroundRuntime(unittest.TestCase):
    def setUp(self):
        bg._last_fetch_at = 0.0
        bg._last_accuracy = {}
        bg._fetch_in_progress = False
        bg._pipeline = None

    def test_social_fetch_fresh(self):
        bg._last_fetch_at = time.time()
        self.assertTrue(bg.social_fetch_fresh(300))
        bg._last_fetch_at = time.time() - 400
        self.assertFalse(bg.social_fetch_fresh(300))

    def test_register_and_sync_fetch(self):
        pipeline = MagicMock()
        pipeline.run_cycle_fetches.return_value = {"outcomes_updated": 1, "trust_updates": 0}
        pipeline.refresh_signals.return_value = []
        pipeline.refresh_cmc_signals.return_value = []
        pipeline.refresh_lc_signals.return_value = []
        bg.register_pipeline(pipeline)

        with patch("services.background_runtime.load_effective_watchlist", return_value=[{"symbol": "BTC/USDT", "active": True}]):
            accuracy = bg.run_social_cycle_sync()

        self.assertEqual(accuracy.get("outcomes_updated"), 1)
        self.assertTrue(bg.social_ever_fetched())
        pipeline.run_cycle_fetches.assert_called_once()

    def test_request_social_fetch_async(self):
        pipeline = MagicMock()
        pipeline.run_cycle_fetches.return_value = {}
        pipeline.refresh_signals.return_value = []
        pipeline.refresh_cmc_signals.return_value = []
        pipeline.refresh_lc_signals.return_value = []
        bg.register_pipeline(pipeline)

        with patch("services.background_runtime.load_effective_watchlist", return_value=[]):
            self.assertTrue(bg.request_social_fetch())
            deadline = time.time() + 3
            while time.time() < deadline and not bg.social_ever_fetched():
                time.sleep(0.05)
        self.assertTrue(bg.social_ever_fetched())


class TestDedup(unittest.TestCase):
    def test_try_claim_id_memory_fallback(self):
        from bus.dedup import clear_memory, try_claim_id

        clear_memory()
        self.assertTrue(try_claim_id("test", "post1", ttl_sec=60))
        self.assertFalse(try_claim_id("test", "post1", ttl_sec=60))


if __name__ == "__main__":
    unittest.main()