import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from x_analyzer import XAnalyzer, XSignal
from x_data_provider import RawPost


class TestXParseBatch(unittest.TestCase):
    def _signal_json(self, post_id, coin="SOL", action="BUY", confidence=85):
        return {
            "post_id": post_id,
            "coin": coin,
            "action": action,
            "confidence": confidence,
            "price_target": None,
            "stop_loss": None,
            "rationale": "test",
        }

    @patch("x_analyzer.ask_grok_json")
    def test_parse_tweets_batch_single_call_for_multiple_posts(self, mock_grok):
        mock_grok.return_value = json.dumps([
            self._signal_json("p1", "BTC", "BUY"),
            self._signal_json("p2", "ETH", "SELL"),
        ])
        analyzer = XAnalyzer()
        posts = [
            RawPost("p1", "Trader1", "buy btc"),
            RawPost("p2", "Trader1", "sell eth"),
        ]
        parsed = analyzer.parse_tweets_batch(posts)
        self.assertEqual(mock_grok.call_count, 1)
        self.assertEqual(parsed["p1"].coin, "BTC")
        self.assertEqual(parsed["p2"].action, "SELL")

    @patch("x_analyzer.ask_grok_json")
    def test_parse_tweets_batch_chunks_large_input(self, mock_grok):
        mock_grok.side_effect = [
            json.dumps([self._signal_json(f"p{i}") for i in range(10)]),
            json.dumps([self._signal_json(f"p{i}") for i in range(10, 12)]),
        ]
        analyzer = XAnalyzer()
        analyzer._parse_batch_size = 10
        posts = [RawPost(f"p{i}", "Trader1", f"tweet {i}") for i in range(12)]
        parsed = analyzer.parse_tweets_batch(posts)
        self.assertEqual(mock_grok.call_count, 2)
        self.assertEqual(len(parsed), 12)

    @patch("x_analyzer.ask_grok_json")
    def test_parse_cache_skips_grok_for_known_post_id(self, mock_grok):
        analyzer = XAnalyzer()
        analyzer._parse_cache["cached1"] = XSignal("Trader1", "BTC", "BUY", 90, post_id="cached1")
        posts = [RawPost("cached1", "Trader1", "already parsed")]
        parsed = analyzer.parse_tweets_batch(posts)
        mock_grok.assert_not_called()
        self.assertEqual(parsed["cached1"].coin, "BTC")

    @patch("x_analyzer.ask_grok_json")
    def test_batch_fallback_to_single_parse_on_partial_response(self, mock_grok):
        mock_grok.side_effect = [
            json.dumps([self._signal_json("p1")]),
            json.dumps(self._signal_json("p2", "ETH", "SELL")),
        ]
        analyzer = XAnalyzer()
        posts = [
            RawPost("p1", "Trader1", "buy btc"),
            RawPost("p2", "Trader1", "sell eth"),
        ]
        parsed = analyzer.parse_tweets_batch(posts)
        self.assertEqual(mock_grok.call_count, 2)
        self.assertEqual(parsed["p2"].coin, "ETH")


if __name__ == "__main__":
    unittest.main()