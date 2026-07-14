import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from bus.ohlcv_cache import OhlcvCache, reset_ohlcv_cache_for_tests, ttl_for_timeframe
from services.market_service import MarketService


def _bars(n: int = 30, start: float = 100.0) -> list:
    out = []
    for i in range(n):
        ts = 1_700_000_000_000 + i * 3_600_000
        price = start + i
        out.append([ts, price, price + 1, price - 1, price, 1000.0 + i])
    return out


class TestOhlcvCache(unittest.TestCase):
    def setUp(self):
        reset_ohlcv_cache_for_tests()
        MarketService.reset_exchange_cache_for_tests()

    def test_ttl_for_timeframe_defaults(self):
        self.assertEqual(ttl_for_timeframe("15m"), 60.0)
        self.assertEqual(ttl_for_timeframe("4h"), 120.0)

    def test_ram_cache_hit(self):
        cache = OhlcvCache(config_raw={"architecture": {}})
        cache.set("BTC/USDT", "4h", 100, _bars(), exchange="gate")
        first = cache.get("BTC/USDT", "4h", 100)
        second = cache.get("BTC/USDT", "4h", 100)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        stats = cache.stats()
        self.assertEqual(stats["hits"], 2)
        self.assertEqual(stats["misses"], 0)

    def test_cache_miss_different_limit(self):
        cache = OhlcvCache(config_raw={"architecture": {}})
        cache.set("ETH/USDT", "1h", 50, _bars(), exchange="gate")
        self.assertIsNone(cache.get("ETH/USDT", "1h", 100))


class TestMarketServiceOhlcvCache(unittest.TestCase):
    def setUp(self):
        reset_ohlcv_cache_for_tests()
        MarketService.reset_exchange_cache_for_tests()

    def test_fetch_ohlcv_uses_cache_on_second_call(self):
        cfg = {"architecture": {"ohlcv_cache_enabled": True}}
        ms = MarketService(config_raw=cfg)
        bars = _bars(40)
        sym = "TESTOHLCV/USDT"  # unique symbol to avoid cross-test cache pollution

        class FakeExchange:
            def __init__(self):
                self.calls = 0
            def fetch_ohlcv(self, symbol, timeframe=None, limit=None):
                self.calls += 1
                return bars

        fake = FakeExchange()

        # Control the cache hit/miss explicitly via a mock returned by from_config
        mock_cache = MagicMock()
        mock_cache.get.side_effect = [None, MagicMock(bars=bars)]

        with patch.object(MarketService, "_get_spot_exchange", return_value=fake), \
             patch("bus.ohlcv_cache.ohlcv_cache_enabled", return_value=True), \
             patch("bus.ohlcv_cache.ohlcv_cache_from_config", return_value=mock_cache):
            df1 = ms._fetch_ohlcv(sym, "4h", 100)
            df2 = ms._fetch_ohlcv(sym, "4h", 100)

        self.assertIsNotNone(df1)
        self.assertIsNotNone(df2)
        self.assertEqual(fake.calls, 1)

    def test_fetch_funding_rate_cached(self):
        cfg = {"architecture": {"funding_cache_ttl_sec": 300}}
        ms = MarketService(config_raw=cfg)

        class FakeSwap:
            calls = 0

            def __init__(self):
                self.has = {"fetchFundingRate": True}

            def fetch_funding_rate(self, symbol):
                FakeSwap.calls += 1
                return {"fundingRate": 0.0001}

        with patch.object(MarketService, "_get_swap_exchange", return_value=FakeSwap()):
            r1 = ms.fetch_funding_rate("ETH/USDT")
            r2 = ms.fetch_funding_rate("ETH/USDT")

        self.assertAlmostEqual(r1, 0.01)
        self.assertAlmostEqual(r2, 0.01)
        self.assertEqual(FakeSwap.calls, 1)


if __name__ == "__main__":
    unittest.main()