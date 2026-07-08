import time
import unittest
from unittest.mock import MagicMock, patch

from data.lunarcrush_scorer import LunarCrushSignal
from services.social_pipeline import SocialPipeline


class TestSocialPipelineLcInterval(unittest.TestCase):
    def _pipeline(self) -> SocialPipeline:
        analyzer = MagicMock()
        analyzer.accounts = []
        return SocialPipeline(analyzer)

    @patch("core.config.get_bot_config")
    @patch("services.social_pipeline.load_effective_watchlist")
    def test_skips_lc_api_within_fetch_interval(self, mock_watchlist, mock_cfg):
        mock_watchlist.return_value = [{"symbol": "BTC/USDT"}]
        mock_cfg.return_value.lunarcrush_config = {
            "enabled": True,
            "fetch_interval_sec": 1800,
            "cache_ttl_sec": 1800,
            "thresholds": {},
            "trust_score": 72,
        }

        pipeline = self._pipeline()
        pipeline.lc_provider.fetch_for_watchlist = MagicMock(return_value=[])
        pipeline._cycle_lc_signals = [
            LunarCrushSignal("BTC", "BUY", 70, post_id="lc_BTC_2026070312")
        ]
        pipeline._last_lc_fetch_at = time.time()

        result = pipeline.process_lc_signals()

        pipeline.lc_provider.fetch_for_watchlist.assert_not_called()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].coin, "BTC")

    @patch("core.config.get_bot_config")
    @patch("services.social_pipeline.load_effective_watchlist")
    @patch("services.social_pipeline.load_lc_signals")
    def test_fetches_lc_after_interval_elapsed(self, mock_lc_store, mock_watchlist, mock_cfg):
        mock_watchlist.return_value = [{"symbol": "BTC/USDT"}]
        mock_lc_store.return_value = {"signals": []}
        mock_cfg.return_value.lunarcrush_config = {
            "enabled": True,
            "fetch_interval_sec": 60,
            "cache_ttl_sec": 60,
            "thresholds": {},
            "trust_score": 72,
        }

        pipeline = self._pipeline()
        pipeline.lc_provider.fetch_for_watchlist = MagicMock(return_value=[])
        pipeline._last_lc_fetch_at = time.time() - 120

        pipeline.process_lc_signals()

        pipeline.lc_provider.fetch_for_watchlist.assert_called_once()

    @patch("core.config.get_bot_config")
    @patch("services.social_pipeline.load_effective_watchlist")
    def test_force_lc_fetch_bypasses_interval(self, mock_watchlist, mock_cfg):
        mock_watchlist.return_value = [{"symbol": "BTC/USDT"}]
        mock_cfg.return_value.lunarcrush_config = {
            "enabled": True,
            "fetch_interval_sec": 1800,
            "cache_ttl_sec": 1800,
            "thresholds": {},
            "trust_score": 72,
        }

        pipeline = self._pipeline()
        pipeline.lc_provider.fetch_for_watchlist = MagicMock(return_value=[])
        pipeline._last_lc_fetch_at = time.time()

        pipeline.process_lc_signals(force=True)

        pipeline.lc_provider.fetch_for_watchlist.assert_called_once()


if __name__ == "__main__":
    unittest.main()