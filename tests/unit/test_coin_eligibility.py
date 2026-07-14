"""Unit tests for per-profile coin eligibility filters."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.coin_eligibility import (
    filter_watchlist_coins,
    passes_coin_filters,
    should_include_trending_overlay,
)
from core.models import MarketContext
from core.trading_profiles import apply_effective_config, coin_filters_config


def _conservative_cfg() -> dict:
    return apply_effective_config({}, {"trading_profile": "conservative"})


def _aggressive_cfg() -> dict:
    return apply_effective_config({}, {"trading_profile": "aggressive"})


class TestPassesCoinFilters(unittest.TestCase):
    def test_disabled_filters_always_pass(self):
        cfg = {"coin_filters": {"enabled": False}}
        ok, reason = passes_coin_filters({"symbol": "PEPE/USDT"}, None, cfg)
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    @patch("data.cmc_market_cap.resolve_market_cap_usd", return_value=100_000_000.0)
    def test_conservative_blocks_high_atr(self, _mcap):
        coin = {"symbol": "ETH/USDT", "atr_pct": 6.0}
        market = MarketContext(symbol="ETH/USDT", timeframe="4h", current_price=100.0, atr_pct=6.0)
        ok, reason = passes_coin_filters(coin, market, _conservative_cfg(), context="buy")
        self.assertFalse(ok)
        self.assertIn("ATR", reason)

    @patch("data.cmc_market_cap.resolve_market_cap_usd", return_value=100_000_000.0)
    def test_conservative_blocks_meme_class(self, _mcap):
        coin = {"symbol": "PEPE/USDT", "atr_pct": 2.0, "strategy_params": {"description": "meme coin"}}
        market = MarketContext(symbol="PEPE/USDT", timeframe="4h", current_price=1.0, atr_pct=2.0)
        ok, reason = passes_coin_filters(coin, market, _conservative_cfg(), context="buy")
        self.assertFalse(ok)
        self.assertIn("meme", reason)

    @patch("data.cmc_market_cap.resolve_market_cap_usd", return_value=100_000_000.0)
    def test_conservative_blocks_trending_source(self, _mcap):
        coin = {"symbol": "SOL/USDT", "source": "cmc_trending", "atr_pct": 2.0}
        market = MarketContext(
            symbol="SOL/USDT",
            timeframe="4h",
            current_price=100.0,
            atr_pct=2.0,
            strategy_params={"volatility_tier": "stable"},
        )
        ok, reason = passes_coin_filters(coin, market, _conservative_cfg(), context="buy")
        self.assertFalse(ok)
        self.assertIn("cmc_trending", reason)

    @patch("data.cmc_market_cap.resolve_market_cap_usd", return_value=1_000_000.0)
    def test_conservative_blocks_low_market_cap(self, _mcap):
        coin = {"symbol": "SMALL/USDT", "atr_pct": 2.0}
        market = MarketContext(
            symbol="SMALL/USDT",
            timeframe="4h",
            current_price=1.0,
            atr_pct=2.0,
            strategy_params={"volatility_tier": "stable"},
        )
        ok, reason = passes_coin_filters(coin, market, _conservative_cfg(), context="buy")
        self.assertFalse(ok)
        self.assertIn("market cap", reason.lower())

    @patch("data.cmc_market_cap.resolve_market_cap_usd", return_value=100_000_000.0)
    def test_conservative_allows_stable_large_cap(self, _mcap):
        coin = {"symbol": "BTC/USDT", "atr_pct": 2.0}
        market = MarketContext(
            symbol="BTC/USDT",
            timeframe="4h",
            current_price=50000.0,
            atr_pct=2.0,
            strategy_params={"volatility_tier": "stable"},
        )
        ok, reason = passes_coin_filters(coin, market, _conservative_cfg(), context="buy")
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    @patch("data.cmc_market_cap.resolve_market_cap_usd", return_value=5_000_000.0)
    def test_aggressive_requires_min_atr(self, _mcap):
        coin = {"symbol": "ALT/USDT", "atr_pct": 2.0}
        market = MarketContext(symbol="ALT/USDT", timeframe="4h", current_price=1.0, atr_pct=2.0)
        ok, reason = passes_coin_filters(coin, market, _aggressive_cfg(), context="buy")
        self.assertFalse(ok)
        self.assertIn("ATR", reason)

    @patch("data.cmc_market_cap.resolve_market_cap_usd", return_value=5_000_000.0)
    def test_aggressive_blocks_stable_tier(self, _mcap):
        coin = {"symbol": "ALT/USDT", "atr_pct": 8.0}
        market = MarketContext(
            symbol="ALT/USDT",
            timeframe="4h",
            current_price=1.0,
            atr_pct=8.0,
            strategy_params={"volatility_tier": "stable"},
        )
        ok, reason = passes_coin_filters(coin, market, _aggressive_cfg(), context="buy")
        self.assertFalse(ok)
        self.assertIn("stable", reason)


class TestWatchlistHelpers(unittest.TestCase):
    def test_should_include_trending_conservative(self):
        self.assertFalse(should_include_trending_overlay(_conservative_cfg()))

    def test_should_include_trending_balanced(self):
        cfg = apply_effective_config({}, {"trading_profile": "balanced"})
        self.assertTrue(should_include_trending_overlay(cfg))

    @patch("core.coin_eligibility.passes_coin_filters")
    def test_filter_watchlist_drops_blocked(self, mock_passes):
        mock_passes.side_effect = [(True, ""), (False, "blocked")]
        coins = [{"symbol": "BTC/USDT"}, {"symbol": "PEPE/USDT"}]
        kept = filter_watchlist_coins(coins, _conservative_cfg())
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["symbol"], "BTC/USDT")


if __name__ == "__main__":
    unittest.main()