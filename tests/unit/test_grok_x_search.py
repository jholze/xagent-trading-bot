import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from grok_x_search import _parse_json_posts, fetch_posts_from_handle
from x_data_provider import GrokXSearchProvider, get_x_provider


class TestGrokXSearch(unittest.TestCase):
    def test_parse_json_posts_from_markdown(self):
        raw = '```json\n[{"text":"buy SOL","created_at":"2026-06-01T12:00:00Z"}]\n```'
        posts = _parse_json_posts(raw)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["text"], "buy SOL")

    @patch("grok_x_search._client")
    def test_fetch_posts_from_handle(self, mock_client_factory):
        mock_response = MagicMock()
        mock_response.output = [
            MagicMock(
                type="message",
                content=[MagicMock(text='[{"post_id":"1","text":"Long ETH","created_at":"2026-06-01T10:00:00Z"}]')],
            )
        ]
        mock_client_factory.return_value.responses.create.return_value = mock_response

        with patch.dict(os.environ, {"XAI_API_KEY": "test-key"}):
            posts = fetch_posts_from_handle("Pentosh1", days=30, max_posts=10)

        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["text"], "Long ETH")

    @patch("x_data_provider.fetch_posts_from_handle")
    def test_grok_provider_maps_raw_posts(self, mock_fetch):
        mock_fetch.return_value = [
            {"post_id": "abc", "text": "Buying BTC", "created_at": "2026-06-01T10:00:00Z"},
            {"post_id": "def", "text": "Short SOL", "created_at": "2026-06-02T10:00:00Z"},
        ]
        provider = GrokXSearchProvider()
        posts = provider.fetch_historical_posts("Pentosh1", days=60)
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0].account, "Pentosh1")

    @patch("x_data_provider.load_x_posts")
    @patch("x_data_provider.fetch_posts_from_handle")
    def test_fetch_new_posts_uses_cache_within_ttl(self, mock_fetch, mock_posts):
        mock_posts.return_value = {"posts": []}
        mock_fetch.return_value = [
            {"post_id": "abc", "text": "Buying BTC", "created_at": "2026-06-01T10:00:00Z"},
        ]
        provider = GrokXSearchProvider({
            "x_performance": {
                "live_search_days": 2,
                "x_search_cache_ttl_sec": 900,
            },
        })
        accounts = [{"handle": "Pentosh1", "enabled": True}]
        first = provider.fetch_new_posts(accounts, limit_per_account=5)
        second = provider.fetch_new_posts(accounts, limit_per_account=5)
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        mock_fetch.assert_called_once()

    @patch("x_data_provider.load_x_posts")
    @patch("x_data_provider.fetch_posts_from_handle")
    def test_fetch_new_posts_uses_live_search_days(self, mock_fetch, mock_posts):
        mock_posts.return_value = {"posts": []}
        mock_fetch.return_value = []
        provider = GrokXSearchProvider({
            "x_performance": {"live_search_days": 2, "x_search_cache_ttl_sec": 0},
        })
        provider.fetch_new_posts([{"handle": "Pentosh1", "enabled": True}])
        mock_fetch.assert_called_once_with("Pentosh1", days=2, max_posts=5)

    def test_get_x_provider_selects_grok_when_configured(self):
        provider = get_x_provider({
            "use_mock_x_data": False,
            "use_grok_x_search": True,
        })
        self.assertIsInstance(provider, GrokXSearchProvider)


if __name__ == "__main__":
    unittest.main()