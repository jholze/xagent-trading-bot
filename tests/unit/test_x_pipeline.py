import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from intelligence.accuracy_tracker import AccuracyTracker
from services.social_pipeline import SocialPipeline
from x_analyzer import XAnalyzer, XSignal
from x_data_provider import RawPost


class TestXPipeline(unittest.TestCase):
    def _valid_grok_json(self, coin="SOL", action="BUY", confidence=85):
        return json.dumps({
            "coin": coin,
            "action": action,
            "confidence": confidence,
            "price_target": 200,
            "stop_loss": 150,
            "rationale": "Strong breakout",
        })

    @patch("x_analyzer.ask_grok_json")
    def test_parse_tweet_valid_json(self, mock_grok):
        mock_grok.return_value = self._valid_grok_json()
        analyzer = XAnalyzer()
        signal = analyzer.parse_tweet("SOL pumping", "CryptoCapo_")
        self.assertEqual(signal.coin, "SOL")
        self.assertEqual(signal.action, "BUY")
        self.assertEqual(signal.confidence, 85)

    @patch("x_analyzer.ask_grok_json")
    def test_parse_tweet_invalid_json_returns_hold(self, mock_grok):
        mock_grok.return_value = "not json at all"
        analyzer = XAnalyzer()
        signal = analyzer.parse_tweet("garbage", "CryptoCapo_")
        self.assertEqual(signal.coin, "UNKNOWN")
        self.assertEqual(signal.action, "HOLD")
        self.assertEqual(signal.confidence, 40)

    @patch("x_analyzer.ask_grok_json")
    def test_track_and_recommend_low_confidence_ignores(self, mock_grok):
        mock_grok.return_value = self._valid_grok_json(confidence=50)
        analyzer = XAnalyzer()
        rec = analyzer.track_and_recommend("weak signal", "CryptoCapo_", 100.0)
        self.assertEqual(rec["action"], "IGNORE")
        self.assertFalse(rec["recommended"])

    @patch("x_analyzer.ask_grok_json")
    def test_track_and_recommend_accepts_pre_parsed_signal(self, mock_grok):
        analyzer = XAnalyzer()
        signal = XSignal("CryptoCapo_", "SOL", "BUY", 85, rationale="pre-parsed")
        with patch.object(analyzer, "parse_tweet") as mock_parse:
            rec = analyzer.track_and_recommend(
                "SOL long", "CryptoCapo_", 100.0, signal=signal,
            )
            mock_parse.assert_not_called()
        self.assertEqual(rec["coin"], "SOL")
        self.assertEqual(rec["parsed_action"], "BUY")

    @patch("x_analyzer.ask_grok_json")
    def test_track_and_recommend_add_to_watchlist_off_list(self, mock_grok):
        mock_grok.return_value = self._valid_grok_json(coin="NEWCOIN", confidence=80)
        analyzer = XAnalyzer()
        with patch("x_analyzer.load_watchlist", return_value=[{"symbol": "BTC/USDT"}]), \
             patch.object(analyzer, "effective_confidence_threshold", return_value=50.0):
            rec = analyzer.track_and_recommend("NEWCOIN gem", "CryptoCapo_", 0.0)
        self.assertEqual(rec["action"], "ADD_TO_WATCHLIST")
        self.assertTrue(rec["recommended"])

    @patch("services.social_pipeline.send_x_recommendation_message")
    @patch("services.social_pipeline.get_prices")
    @patch("x_analyzer.ask_grok_json")
    def test_process_new_posts_single_batch_parse(self, mock_grok, mock_prices, mock_notify):
        mock_grok.return_value = json.dumps([{
            "post_id": "post_1",
            **json.loads(self._valid_grok_json()),
        }])
        mock_prices.return_value = (150.0, 150.0, None)

        post = RawPost("post_1", "CryptoCapo_", "SOL looking strong", datetime.now().isoformat())
        provider = MagicMock()
        provider.fetch_new_posts.return_value = [post]

        analyzer = XAnalyzer()
        pipeline = SocialPipeline(analyzer)
        pipeline.provider = provider

        with patch.object(pipeline, "_already_logged", return_value=False), \
             patch.object(analyzer, "log_tracked_post"), \
             patch.object(pipeline, "_enqueue_strategy_discovery"):
            with patch.object(analyzer, "parse_tweets_batch", wraps=analyzer.parse_tweets_batch) as mock_batch:
                recs = pipeline.process_new_posts()
                self.assertEqual(mock_batch.call_count, 1)
        self.assertEqual(mock_grok.call_count, 1)
        self.assertEqual(len(recs), 1)
        self.assertEqual(len(pipeline._cycle_signals), 1)

    @patch("services.social_pipeline.send_x_recommendation_message")
    @patch("x_analyzer.ask_grok_json")
    def test_process_new_posts_skips_logged_posts(self, mock_grok, mock_notify):
        analyzer = XAnalyzer()
        pipeline = SocialPipeline(analyzer)
        with patch.object(pipeline, "_already_logged", return_value=True):
            recs = pipeline.process_new_posts()
        self.assertEqual(recs, [])
        mock_grok.assert_not_called()

    @patch("x_analyzer.save_x_posts")
    @patch("x_analyzer.load_x_posts")
    def test_log_tracked_post_persists_price_target_and_stop_loss(self, mock_load, mock_save):
        mock_load.return_value = {"posts": []}
        analyzer = XAnalyzer()
        analyzer.log_tracked_post({
            "post_id": "persist_p1",
            "account": "Trader1",
            "coin": "BTC",
            "action": "BUY",
            "parsed_action": "BUY",
            "confidence": 82,
            "rationale": "TP at 105k",
            "recommended": True,
            "price_target": 105000.0,
            "stop_loss": 98000.0,
        })
        saved = mock_save.call_args[0][0]
        entry = saved["posts"][-1]
        self.assertEqual(entry["price_target"], 105000.0)
        self.assertEqual(entry["stop_loss"], 98000.0)
        self.assertEqual(analyzer._parse_cache["persist_p1"].price_target, 105000.0)
        self.assertEqual(analyzer._parse_cache["persist_p1"].stop_loss, 98000.0)

    @patch("x_analyzer.load_x_posts")
    def test_parse_cache_hydrates_price_target_from_x_posts(self, mock_load):
        mock_load.return_value = {
            "posts": [{
                "post_id": "hydrate_p1",
                "account": "Trader1",
                "coin": "ETH",
                "parsed_action": "SELL",
                "confidence": 77,
                "price_target": 3200.0,
                "stop_loss": 3400.0,
                "rationale": "short to support",
            }],
        }
        analyzer = XAnalyzer()
        cached = analyzer._parse_cache["hydrate_p1"]
        self.assertEqual(cached.price_target, 3200.0)
        self.assertEqual(cached.stop_loss, 3400.0)

    @patch("x_analyzer.ask_grok_json")
    def test_track_and_recommend_includes_price_levels(self, mock_grok):
        mock_grok.return_value = self._valid_grok_json()
        analyzer = XAnalyzer()
        rec = analyzer.track_and_recommend("SOL long", "CryptoCapo_", 150.0)
        self.assertEqual(rec["price_target"], 200)
        self.assertEqual(rec["stop_loss"], 150)

    @patch("services.social_pipeline.send_x_recommendation_message")
    @patch("services.social_pipeline.get_prices")
    @patch("x_analyzer.ask_grok_json")
    def test_process_new_posts_persists_price_levels(self, mock_grok, mock_prices, mock_notify):
        mock_grok.return_value = self._valid_grok_json(coin="SOL", action="BUY", confidence=85)
        mock_prices.return_value = (150.0, 150.0, None)

        post = RawPost("post_levels_persist_test", "CryptoCapo_", "SOL tp 200", datetime.now().isoformat())
        provider = MagicMock()
        provider.fetch_new_posts.return_value = [post]

        analyzer = XAnalyzer()
        pipeline = SocialPipeline(analyzer)
        pipeline.provider = provider

        with patch.object(pipeline, "_already_logged", return_value=False), \
             patch.object(analyzer, "log_tracked_post", wraps=analyzer.log_tracked_post) as mock_log, \
             patch.object(pipeline, "_enqueue_strategy_discovery"):
            pipeline.process_new_posts()

        logged = mock_log.call_args[0][0]
        self.assertEqual(logged["price_target"], 200)
        self.assertEqual(logged["stop_loss"], 150)

    def test_score_signal_consensus_multiplier(self):
        analyzer = XAnalyzer()
        signals = [
            XSignal("a1", "SOL", "BUY", 80),
            XSignal("a2", "SOL", "BUY", 75),
        ]
        for s in signals:
            s.trust_score = 70
            s.effective_confidence = s.confidence * 0.7
        score = analyzer.score_signal(signals[0], 50.0, all_signals=signals)
        self.assertGreater(score, 0)
        self.assertGreater(signals[0].score, 0)

    def test_accuracy_tracker_evaluates_buy_outcome(self):
        tracker = AccuracyTracker({"accuracy": {"buy_success_pct": 3.0, "sell_success_pct": -2.0}})
        post = {
            "coin": "SOL",
            "parsed_action": "BUY",
            "signal_price": 100.0,
            "timestamp": (datetime.now() - timedelta(hours=25)).isoformat(),
        }
        self.assertTrue(tracker._evaluate_post(post, 5.0))
        self.assertFalse(tracker._evaluate_post(post, 1.0))

    @patch("intelligence.accuracy_tracker.get_prices")
    def test_accuracy_tracker_update_outcomes(self, mock_prices):
        mock_prices.return_value = (110.0, 110.0, None)
        old_posts = {
            "posts": [{
                "post_id": "acc_test_1",
                "coin": "SOL",
                "parsed_action": "BUY",
                "signal_price": 100.0,
                "timestamp": (datetime.now() - timedelta(hours=25)).isoformat(),
            }]
        }
        with patch("intelligence.accuracy_tracker.load_x_posts", return_value=old_posts), \
             patch("intelligence.accuracy_tracker.save_x_posts") as mock_save:
            tracker = AccuracyTracker()
            updated = tracker.update_outcomes()
            self.assertGreater(updated, 0)
            self.assertIsNotNone(old_posts["posts"][0].get("outcome_24h"))
            mock_save.assert_called()

    @patch("intelligence.accuracy_tracker.save_x_accounts")
    @patch("intelligence.accuracy_tracker.load_x_accounts")
    @patch("intelligence.accuracy_tracker.load_x_posts")
    def test_accuracy_tracker_updates_trust_after_three_samples(
        self, mock_posts, mock_accounts, mock_save_accounts,
    ):
        posts = [
            {
                "account": "Trader1",
                "was_correct": True,
                "outcome_24h": 5.0,
                "timestamp": datetime.now().isoformat(),
            }
        ] * 3
        mock_posts.return_value = {"posts": posts}
        mock_accounts.return_value = [{"handle": "Trader1", "trust_score": 70, "enabled": True}]
        tracker = AccuracyTracker({"accuracy": {"trust_ema_alpha": 0.3}})
        updated = tracker.update_trust_scores()
        self.assertEqual(updated, 1)
        mock_save_accounts.assert_called()


if __name__ == "__main__":
    unittest.main()