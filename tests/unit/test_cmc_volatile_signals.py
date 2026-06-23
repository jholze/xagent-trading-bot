import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.config import BotConfig
from data.cmc_community_provider import CMCCommunityParser, RawCMCPost
from data.cmc_volatile_signals import CMCVolatileSignalAggregator
from data_manager import trending_watchlist_live_enabled
from strategies.decision_engine import DecisionEngine
from strategies.registry import resolve_strategy_params
from core.models import MarketContext


class TestCMCTrendingLive(unittest.TestCase):
    def test_trending_watchlist_live_enabled(self):
        raw = {
            "cmc": {"trending_watchlist": {"enabled": True, "live_enabled": True}},
            "live": {"dry_run": True, "dry_run_enhanced": False},
        }
        self.assertTrue(trending_watchlist_live_enabled(raw))

    def test_trending_watchlist_disabled_when_live_off(self):
        raw = {
            "cmc": {"trending_watchlist": {"enabled": True, "live_enabled": False}},
            "live": {"dry_run": True},
        }
        self.assertFalse(trending_watchlist_live_enabled(raw))


class TestCMCVolatileAggregator(unittest.TestCase):
    def test_apply_tier_trust_scores(self):
        agg = CMCVolatileSignalAggregator(api_key="")
        parser = CMCCommunityParser()
        post = RawCMCPost(
            post_id="t1",
            coin="PEPE",
            text="trending",
            votes_bullish=70,
            votes_bearish=30,
        )
        post.trending_rank = 3
        post.signal_tier = "trending"
        signal = parser.parse(post)
        from data.cmc_volatile_signals import _apply_tier

        _apply_tier(signal, "trending", trending_rank=3)
        self.assertEqual(signal.signal_tier, "trending")
        self.assertEqual(signal.trending_rank, 3)
        self.assertEqual(signal.trust_score, 72.0)


class TestCMCTrendingFusion(unittest.TestCase):
    def test_trending_only_buy_when_conditions_met(self):
        engine = DecisionEngine()
        cfg = BotConfig()
        cfg._raw = {
            "cmc": {
                "cmc_trending_fusion": {
                    "enabled": True,
                    "allow_cmc_only_buy_top_n": 8,
                    "cmc_only_buy_min_confidence": 58,
                    "block_buy_if_rsi_above": 68,
                    "require_volatile_atr_tier": True,
                }
            },
            "volatile_altcoin": {"enabled": True},
        }
        engine.config = cfg

        class Sig:
            action = "BUY"
            signal_tier = "trending"
            trending_rank = 4
            confidence = 62

        market = MarketContext(
            symbol="PEPE/USDT",
            timeframe="1h",
            current_price=1.0,
            rsi=55.0,
            has_position=False,
            open_positions=0,
            strategy_params={
                "strategy_profile": "volatile_altcoin",
                "volatility_tier": "volatile",
            },
        )
        self.assertTrue(engine._cmc_trending_only_buy(Sig(), market, market.strategy_params))

    def test_cmc_trending_gets_volatile_profile_before_entry(self):
        cfg = BotConfig()
        cfg._raw = {
            "volatile_altcoin": {
                "enabled": True,
                "timeframe": "1h",
                "cmc_min_confidence": 50,
            },
            "cmc": {},
        }
        with patch("strategies.registry.get_bot_config", return_value=cfg), \
             patch("strategies.registry._resolve_volatility_tier", return_value="volatile"):
            params = resolve_strategy_params(
                {"symbol": "PEPE/USDT", "timeframe": "1h", "source": "cmc_trending"},
                has_position=False,
                atr_pct=8.0,
            )
        self.assertEqual(params.get("strategy_profile"), "volatile_altcoin")
        self.assertEqual(params.get("cmc_min_confidence"), 50)


if __name__ == "__main__":
    unittest.main()