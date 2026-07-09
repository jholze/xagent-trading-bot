import json
import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from bus.price_cache import RedisPriceCache, reset_price_cache_for_tests
from services.coin_query_service import normalize_symbols, query_coin_prices, webhook_token_ok


class TestRedisPriceCache(unittest.TestCase):
    def setUp(self):
        reset_price_cache_for_tests()

    def test_set_and_get_many(self):
        client = MagicMock()
        stored = {}

        def _setex(key, ttl, value):
            stored[key] = value

        def _mget(keys):
            return [stored.get(k) for k in keys]

        client.setex.side_effect = _setex
        client.mget.side_effect = _mget
        client.pipeline.return_value = client
        client.execute.return_value = True
        client.ping.return_value = True

        cache = RedisPriceCache(key_prefix="aria:", ttl_sec=60)
        with patch.object(cache, "_client", return_value=client):
            self.assertTrue(cache.available())
            written = cache.set_many(
                {"BTC/USDT": 64000.0, "ETH/USDT": 1800.0},
                sources={"BTC/USDT": "live", "ETH/USDT": "live"},
            )
            self.assertEqual(written, 2)
            found = cache.get_many(["BTC/USDT", "ETH/USDT", "SOL/USDT"])
        self.assertIn("BTC/USDT", found)
        self.assertAlmostEqual(found["BTC/USDT"].price, 64000.0)
        self.assertEqual(found["BTC/USDT"].source, "live")

    def test_get_many_ignores_invalid_payload(self):
        client = MagicMock()
        client.mget.return_value = [json.dumps({"price": 0}), "not-json"]
        cache = RedisPriceCache(key_prefix="test:")
        with patch.object(cache, "_client", return_value=client):
            found = cache.get_many(["BTC/USDT", "ETH/USDT"])
        self.assertEqual(found, {})


class TestCoinQueryService(unittest.TestCase):
    def setUp(self):
        reset_price_cache_for_tests()

    def test_normalize_symbols(self):
        self.assertEqual(normalize_symbols("btc, eth"), ["BTC/USDT", "ETH/USDT"])
        self.assertEqual(normalize_symbols(["SOL/USDT"]), ["SOL/USDT"])

    def test_webhook_token_optional_when_unset(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(webhook_token_ok(None, {"architecture": {}}))
        with patch.dict(os.environ, {"COIN_WEBHOOK_TOKEN": "secret"}, clear=False):
            self.assertFalse(webhook_token_ok("wrong", {}))
            self.assertTrue(webhook_token_ok("secret", {}))

    def test_query_uses_redis_hits(self):
        cfg = {"architecture": {"price_cache_enabled": True, "price_cache_ttl_sec": 120}}
        mock_cache = MagicMock()
        mock_cache.available.return_value = True
        from bus.price_cache import CachedPrice

        mock_cache.get_many.return_value = {
            "BTC/USDT": CachedPrice("BTC/USDT", 65000.0, "live", time.time()),
        }
        with patch("services.coin_query_service.price_cache_from_config", return_value=mock_cache), \
             patch("services.coin_query_service.get_prices_batch") as mock_batch:
            result = query_coin_prices(["BTC/USDT", "ETH/USDT"], config_raw=cfg)
        self.assertEqual(result.cache_hits, 1)
        self.assertEqual(result.prices["BTC/USDT"].source, "redis")
        mock_batch.assert_called_once()


class TestResolveRedisUrl(unittest.TestCase):
    def test_env_overrides_localhost_config(self):
        from bus.redis_client import resolve_redis_url, reset_redis_client

        reset_redis_client()
        with patch.dict(os.environ, {"REDIS_URL": "redis://railway.internal:6379/0"}, clear=False):
            url = resolve_redis_url("redis://127.0.0.1:6379/0")
        self.assertEqual(url, "redis://railway.internal:6379/0")

    def test_config_used_when_no_env(self):
        from bus.redis_client import resolve_redis_url

        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("REDIS_URL", None)
            os.environ.pop("ARCHITECTURE_REDIS_URL", None)
            url = resolve_redis_url("redis://127.0.0.1:6379/0")
        self.assertEqual(url, "redis://127.0.0.1:6379/0")


if __name__ == "__main__":
    unittest.main()