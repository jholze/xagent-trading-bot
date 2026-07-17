"""CMC Basic-plan quotes_fallback_as_signal filter (issue #7)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from data.cmc_capabilities import (
    format_cmc_status_line,
    trade_path_mode,
)
from data.cmc_community_provider import CMCCommunitySignal
from services.social_pipeline import SocialPipeline


class TestTradePathMode(unittest.TestCase):
    def test_quotes_blocked_when_fallback_off(self):
        caps = {
            "api_key_set": True,
            "plan_label": "Basic",
            "endpoints": {
                "listings/latest": True,
                "quotes/latest": True,
                "community/trending/token": False,
                "content/latest": False,
                "trending/latest": False,
                "trending/gainers-losers": False,
            },
        }
        mode = trade_path_mode({"enabled": True, "quotes_fallback_as_signal": False}, caps)
        self.assertEqual(mode, "quotes_blocked")

    def test_quotes_fallback_when_enabled(self):
        caps = {
            "api_key_set": True,
            "plan_label": "Basic",
            "endpoints": {
                "listings/latest": True,
                "quotes/latest": True,
                "community/trending/token": False,
                "content/latest": False,
                "trending/latest": False,
            },
        }
        mode = trade_path_mode({"enabled": True, "quotes_fallback_as_signal": True}, caps)
        self.assertEqual(mode, "quotes_fallback")
        line = format_cmc_status_line({"enabled": True, "quotes_fallback_as_signal": True}, caps)
        self.assertIn("fallback", line.lower())

    def test_market_trending_startup_mode(self):
        caps = {
            "api_key_set": True,
            "plan_label": "Startup",
            "endpoints": {
                "trending/latest": True,
                "trending/gainers-losers": True,
                "listings/latest": True,
                "quotes/latest": True,
                "community/trending/token": False,
                "dex/tokens/trending/list": False,
            },
        }
        mode = trade_path_mode({"enabled": True, "quotes_fallback_as_signal": True}, caps)
        self.assertEqual(mode, "market_trending")
        line = format_cmc_status_line({"enabled": True}, caps)
        self.assertIn("trending", line.lower())
        self.assertIn("dex=off", line)

    def test_community_preferred(self):
        caps = {
            "api_key_set": True,
            "plan_label": "Builder",
            "endpoints": {
                "community/trending/token": True,
                "quotes/latest": True,
                "trending/latest": True,
            },
        }
        self.assertEqual(
            trade_path_mode({"enabled": True, "quotes_fallback_as_signal": False}, caps),
            "community",
        )


class TestQuotesFallbackFilter(unittest.TestCase):
    def _quote_signal(self) -> CMCCommunitySignal:
        sig = CMCCommunitySignal("STG", "BUY", 70, votes_bullish=70, votes_bearish=30)
        sig.quotes_fallback = True
        sig.signal_tier = "quote"
        sig.trust_score = 55.0
        return sig

    def test_process_cmc_drops_quotes_when_disabled(self):
        post = SimpleNamespace(
            post_id="cmc_quote_STG_bull_2026-07-17",
            coin="STG",
            text="STG +8% in 24h",
            author="CMC Market",
            votes_bullish=70,
            votes_bearish=30,
            created_at="2026-07-17T00:00:00",
            signal_tier="quote",
            trending_rank=0,
        )
        parser = MagicMock()
        parser.parse.return_value = self._quote_signal()
        provider = MagicMock()
        provider.fetch_posts.return_value = [post]
        pipeline = SocialPipeline(analyzer=MagicMock())
        pipeline.cmc_provider = provider
        pipeline.cmc_parser = parser

        cfg = MagicMock()
        cfg.cmc_config = {
            "enabled": True,
            "quotes_fallback_as_signal": False,
            "quotes_fallback_trust_score": 55,
            "trust_score": 65,
        }
        with patch("core.config.get_bot_config", return_value=cfg), \
             patch("services.social_pipeline.load_effective_watchlist", return_value=[]), \
             patch("services.social_pipeline.load_cmc_posts", return_value={"posts": []}), \
             patch.object(pipeline, "_claim_post", return_value=False):
            out = pipeline.process_cmc_posts()
        self.assertEqual(out, [])

    def test_process_cmc_keeps_quotes_when_enabled(self):
        post = SimpleNamespace(
            post_id="cmc_quote_STG_bull_2026-07-17",
            coin="STG",
            text="STG +8% in 24h",
            author="CMC Market",
            votes_bullish=70,
            votes_bearish=30,
            created_at="2026-07-17T00:00:00",
            signal_tier="quote",
            trending_rank=0,
        )
        sig = self._quote_signal()
        parser = MagicMock()
        parser.parse.return_value = sig
        provider = MagicMock()
        provider.fetch_posts.return_value = [post]
        pipeline = SocialPipeline(analyzer=MagicMock())
        pipeline.cmc_provider = provider
        pipeline.cmc_parser = parser

        cfg = MagicMock()
        cfg.cmc_config = {
            "enabled": True,
            "quotes_fallback_as_signal": True,
            "quotes_fallback_trust_score": 55,
            "trust_score": 65,
        }
        with patch("core.config.get_bot_config", return_value=cfg), \
             patch("services.social_pipeline.load_effective_watchlist", return_value=[]), \
             patch("services.social_pipeline.load_cmc_posts", return_value={"posts": []}), \
             patch.object(pipeline, "_claim_post", return_value=False):
            out = pipeline.process_cmc_posts()
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].quotes_fallback)
        self.assertEqual(out[0].trust_score, 55.0)

    def test_decision_engine_ignores_quotes_when_disabled(self):
        from strategies.decision_engine import DecisionEngine

        engine = DecisionEngine()
        cmc = self._quote_signal()
        cmc.action = "SELL"
        cmc.confidence = 90
        pos = {"amount": 1000, "average_entry": 1.0}
        cmc_cfg = {
            "quotes_fallback_as_signal": False,
            "sell_min_confidence": 70,
            "sell_requires_ta": False,
            "quotes_fallback_sell_threshold_bonus": 10,
        }
        with patch.object(type(engine.config), "cmc_config", property(lambda self: cmc_cfg)), \
             patch.object(engine.market, "fetch_indicators", return_value={
                 "rsi": 75.0, "lower_bb": 0.9, "vol_multiplier": 1.2,
             }), \
             patch("strategies.decision_engine.get_position", return_value=pos):
            analysis = engine.evaluate(
                {"symbol": "STG/USDT", "timeframe": "4h", "source": "cmc_trending"},
                1.0,
                cmc_signals=[cmc],
            )
        self.assertNotIn("cmc", analysis.sources)


if __name__ == "__main__":
    unittest.main()
