import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from data.cmc_trending_provider import CMCTrendingProvider


class TestCMCTrendingProvider(unittest.TestCase):
    def test_fetch_trending_latest_primary(self):
        provider = CMCTrendingProvider(api_key="test-key")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": [{"symbol": "PEPE"}, {"symbol": "DOGE"}],
        }
        with patch("data.cmc_trending_provider.requests.get", return_value=mock_resp):
            symbols, source = provider.fetch_trending_symbols(limit=5)
        self.assertEqual(symbols, ["PEPE", "DOGE"])
        self.assertEqual(source, "trending/latest")

    def test_fallback_to_gainers_losers(self):
        provider = CMCTrendingProvider(api_key="test-key")
        fail_resp = MagicMock(status_code=403, json=lambda: {"status": {"error_message": "plan"}})
        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.json.return_value = {
            "data": {
                "gainers": [{"symbol": "WIF"}],
                "losers": [{"symbol": "BONK"}],
            },
        }

        def side_effect(url, **kwargs):
            if "trending/latest" in url:
                return fail_resp
            return ok_resp

        with patch("data.cmc_trending_provider.requests.get", side_effect=side_effect):
            symbols, source = provider.fetch_trending_symbols(limit=5)
        self.assertEqual(symbols, ["WIF", "BONK"])
        self.assertEqual(source, "trending/gainers-losers")

    def test_fallback_to_listings_movers(self):
        provider = CMCTrendingProvider(api_key="test-key")
        fail_resp = MagicMock(status_code=403, json=lambda: {"status": {"error_message": "plan"}})
        listings_resp = MagicMock()
        listings_resp.status_code = 200
        listings_resp.json.return_value = {
            "data": [
                {"symbol": "LOW", "quote": {"USD": {"percent_change_24h": 1}}},
                {"symbol": "HIGH", "quote": {"USD": {"percent_change_24h": 25}}},
            ],
        }

        def side_effect(url, **kwargs):
            if "listings/latest" in url:
                return listings_resp
            return fail_resp

        with patch("data.cmc_trending_provider.requests.get", side_effect=side_effect):
            symbols, source = provider.fetch_trending_symbols(limit=5)
        self.assertEqual(symbols[0], "HIGH")
        self.assertEqual(source, "listings/latest")

    def test_empty_without_api_key(self):
        with patch.dict(os.environ, {}, clear=True):
            provider = CMCTrendingProvider(api_key="")
            symbols, source = provider.fetch_trending_symbols()
        self.assertEqual(symbols, [])
        self.assertEqual(source, "")


if __name__ == "__main__":
    unittest.main()