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

        with patch("services.background_runtime._ensure_trending_watchlist"), \
             patch("services.background_runtime.load_effective_watchlist", return_value=[{"symbol": "BTC/USDT", "active": True}]):
            accuracy = bg.run_social_cycle_sync()

        self.assertEqual(accuracy.get("outcomes_updated"), 1)
        self.assertTrue(bg.social_ever_fetched())
        pipeline.run_cycle_fetches.assert_called_once()

    def test_sync_fetch_refreshes_trending_when_watchlist_missing(self):
        pipeline = MagicMock()
        pipeline.run_cycle_fetches.return_value = {}
        pipeline.refresh_signals.return_value = []
        pipeline.refresh_cmc_signals.return_value = []
        pipeline.refresh_lc_signals.return_value = []
        bg.register_pipeline(pipeline)

        with patch("services.background_runtime._ensure_trending_watchlist") as mock_sync, \
             patch("services.background_runtime.load_effective_watchlist", return_value=[]):
            bg.run_social_cycle_sync()

        mock_sync.assert_called_once()

    def test_sync_fetch_skips_trending_when_watchlist_provided(self):
        pipeline = MagicMock()
        pipeline.run_cycle_fetches.return_value = {}
        pipeline.refresh_signals.return_value = []
        pipeline.refresh_cmc_signals.return_value = []
        pipeline.refresh_lc_signals.return_value = []
        bg.register_pipeline(pipeline)
        provided = [{"symbol": "ETH/USDT", "active": True}]

        with patch("services.background_runtime._ensure_trending_watchlist") as mock_sync:
            bg.run_social_cycle_sync(watchlist=provided)

        mock_sync.assert_not_called()
        pipeline.run_cycle_fetches.assert_called_once_with(provided)

    def test_request_social_fetch_async(self):
        pipeline = MagicMock()
        pipeline.run_cycle_fetches.return_value = {}
        pipeline.refresh_signals.return_value = []
        pipeline.refresh_cmc_signals.return_value = []
        pipeline.refresh_lc_signals.return_value = []
        bg.register_pipeline(pipeline)

        with patch("services.background_runtime._ensure_trending_watchlist"), \
             patch("services.background_runtime.load_effective_watchlist", return_value=[]):
            self.assertTrue(bg.request_social_fetch())
            deadline = time.time() + 3
            while time.time() < deadline and not bg.social_ever_fetched():
                time.sleep(0.05)
        self.assertTrue(bg.social_ever_fetched())


class TestNewsPulseTick(unittest.TestCase):
    def setUp(self):
        bg._last_news_pulse_at = 0.0

    def tearDown(self):
        bg._last_news_pulse_at = 0.0

    def test_disabled_flag_is_noop(self):
        cfg = {
            "sell_policy": {
                "correlated_tier": {
                    "news_pulse_enabled": False,
                    "news_pulse_poll_interval_sec": 1,
                }
            }
        }
        with patch("intelligence.memory.news_providers.poll_and_ingest_news") as poll:
            bg._maybe_tick_news_pulse(cfg)
        poll.assert_not_called()
        self.assertEqual(bg._last_news_pulse_at, 0.0)

    def test_enabled_polls_and_caches_then_self_gates(self):
        cfg = {
            "sell_policy": {
                "correlated_tier": {
                    "news_pulse_enabled": True,
                    "news_pulse_poll_interval_sec": 900,
                    "news_pulse_since_minutes": 30,
                }
            }
        }
        pulse = {
            "bearish_score": 0.4,
            "confidence": 0.2,
            "event_count": 1,
            "top_events": [],
        }
        with patch("intelligence.memory.news_providers.poll_and_ingest_news") as poll, patch(
            "intelligence.memory.market_pulse.market_pulse_score",
            return_value=pulse,
        ) as score, patch(
            "intelligence.memory.market_pulse.set_cached_market_pulse"
        ) as cache:
            bg._maybe_tick_news_pulse(cfg)
            bg._maybe_tick_news_pulse(cfg)
        poll.assert_called_once()
        score.assert_called_once()
        cache.assert_called_once_with(pulse)
        self.assertGreater(bg._last_news_pulse_at, 0.0)

    def test_poll_error_still_scores_and_does_not_raise(self):
        cfg = {
            "sell_policy": {
                "correlated_tier": {
                    "news_pulse_enabled": True,
                    "news_pulse_poll_interval_sec": 1,
                }
            }
        }
        with patch(
            "intelligence.memory.news_providers.poll_and_ingest_news",
            side_effect=RuntimeError("rss down"),
        ), patch(
            "intelligence.memory.market_pulse.market_pulse_score",
            return_value={"bearish_score": 0.0, "confidence": 0.0, "event_count": 0, "top_events": []},
        ) as score, patch(
            "intelligence.memory.market_pulse.set_cached_market_pulse"
        ) as cache:
            bg._maybe_tick_news_pulse(cfg)
        score.assert_called_once()
        cache.assert_called_once()


class TestDedup(unittest.TestCase):
    def test_try_claim_id_memory_fallback(self):
        from bus.dedup import clear_memory, try_claim_id
        from unittest.mock import patch

        clear_memory()
        with patch("bus.dedup.get_redis", return_value=None):  # force memory fallback, avoid any redis side effects in batch
            self.assertTrue(try_claim_id("test", "post1", ttl_sec=60))
            self.assertFalse(try_claim_id("test", "post1", ttl_sec=60))


if __name__ == "__main__":
    unittest.main()