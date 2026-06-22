import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from intelligence.x_account_backtest import XAccountBacktester
from notifications.telegram_commands.x_commands import (
    _parse_testaccount_args,
    add_x_account,
    handle,
    handle_callback,
)
from x_data_provider import MockXProvider, RawPost


class TestXAccountBacktest(unittest.TestCase):
    def _valid_grok_json(self, coin="BTC", action="BUY", confidence=85, price_target=None):
        return json.dumps({
            "coin": coin,
            "action": action,
            "confidence": confidence,
            "price_target": price_target,
            "stop_loss": None,
            "rationale": "Strong signal",
        })

    def _batch_grok_response(self, items):
        payloads = []
        for item in items:
            if len(item) == 4:
                pid, coin, action, price_target = item
            else:
                pid, coin, action = item
                price_target = None
            data = json.loads(self._valid_grok_json(coin, action, price_target=price_target))
            data["post_id"] = pid
            payloads.append(data)
        return json.dumps(payloads)

    def _backtest_config(self):
        return {
            "accuracy": {"buy_success_pct": 3.0, "sell_success_pct": -2.0},
            "max_usdt_per_trade": 150,
            "x_backtest": {
                "default_days": 60,
                "max_days": 365,
                "max_posts": 50,
                "max_hold_days": 7,
                "min_signal_age_hours": 24,
            },
        }

    def test_parse_testaccount_args_default_days(self):
        handle, days = _parse_testaccount_args("/testaccount CryptoCapo_")
        self.assertEqual(handle, "CryptoCapo_")
        self.assertEqual(days, 60)

    def test_parse_testaccount_args_custom_days_and_at(self):
        handle, days = _parse_testaccount_args("/testaccount @Pentosh1 30")
        self.assertEqual(handle, "Pentosh1")
        self.assertEqual(days, 30)

    def test_parse_testaccount_args_missing_handle(self):
        handle, days = _parse_testaccount_args("/testaccount")
        self.assertIsNone(handle)

    @patch("intelligence.x_account_backtest.get_indicators_at_time")
    @patch("intelligence.x_account_backtest.get_path_extremes")
    @patch("intelligence.x_account_backtest.get_price_at_time")
    @patch("x_analyzer.ask_grok_json")
    def test_backtest_evaluates_buy_sell_signals(self, mock_grok, mock_price, mock_extremes, mock_indicators):
        mock_grok.return_value = self._batch_grok_response([
            ("p1", "BTC", "BUY"),
            ("p2", "ETH", "SELL"),
            ("p3", "SOL", "HOLD"),
        ])
        mock_price.side_effect = [
            100.0, 104.0, 110.0,
            200.0, 196.0, 190.0,
        ]
        mock_extremes.return_value = (110.0, 90.0)
        mock_indicators.return_value = {"rsi": 35.0, "lower_bb": 99.0, "vol_multiplier": 1.5, "close": 100.0}

        provider = MockXProvider()
        posts = [
            RawPost("p1", "CryptoCapo_", "buy btc", (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()),
            RawPost("p2", "CryptoCapo_", "sell eth", (datetime.now(timezone.utc) - timedelta(days=12)).isoformat()),
            RawPost("p3", "CryptoCapo_", "watching", (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()),
        ]

        backtester = XAccountBacktester(config=self._backtest_config())
        backtester.provider = provider
        with patch.object(provider, "fetch_historical_posts", return_value=posts):
            result = backtester.run("CryptoCapo_", days=60)

        self.assertEqual(result.tweets_fetched, 3)
        self.assertEqual(len(result.signals), 2)
        stats = result.summary_stats()
        self.assertEqual(stats["samples"], 2)
        self.assertEqual(stats["hits_7d"], 2)
        self.assertEqual(stats["hit_rate_7d"], 1.0)
        self.assertEqual(stats["buy"]["count"], 1)
        self.assertEqual(stats["sell"]["count"], 1)

    @patch("intelligence.x_account_backtest.get_indicators_at_time")
    @patch("intelligence.x_account_backtest.get_path_extremes")
    @patch("intelligence.x_account_backtest.check_target_hit")
    @patch("intelligence.x_account_backtest.get_price_at_time")
    @patch("x_analyzer.ask_grok_json")
    def test_backtest_target_hit(self, mock_grok, mock_price, mock_target, mock_extremes, mock_indicators):
        mock_grok.return_value = self._valid_grok_json("BTC", "BUY", price_target=105.0)
        mock_price.side_effect = [100.0, 104.0, 110.0]
        mock_extremes.return_value = (106.0, 98.0)
        mock_target.return_value = True
        mock_indicators.return_value = {"rsi": 40.0, "lower_bb": 98.0, "vol_multiplier": 1.4, "close": 100.0}

        provider = MockXProvider()
        posts = [
            RawPost("p1", "CryptoCapo_", "buy btc tp 105", (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()),
        ]
        backtester = XAccountBacktester(config=self._backtest_config())
        backtester.provider = provider
        with patch.object(provider, "fetch_historical_posts", return_value=posts):
            result = backtester.run("CryptoCapo_", days=60)

        self.assertEqual(len(result.signals), 1)
        self.assertTrue(result.signals[0].has_price_target)
        self.assertTrue(result.signals[0].target_hit)
        self.assertEqual(result.summary_stats()["target"]["hits"], 1)

    @patch("intelligence.x_account_backtest.get_indicators_at_time")
    @patch("intelligence.x_account_backtest.get_path_extremes")
    @patch("intelligence.x_account_backtest.get_price_at_time")
    @patch("x_analyzer.ask_grok_json")
    def test_backtest_buy_misses_below_threshold(self, mock_grok, mock_price, mock_extremes, mock_indicators):
        mock_grok.return_value = self._valid_grok_json("BTC", "BUY")
        mock_price.side_effect = [100.0, 101.0, 101.5]
        mock_extremes.return_value = (102.0, 99.0)
        mock_indicators.return_value = {"rsi": 55.0, "lower_bb": 95.0, "vol_multiplier": 1.0, "close": 100.0}

        provider = MockXProvider()
        posts = [
            RawPost("p1", "CryptoCapo_", "buy btc", (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()),
        ]
        backtester = XAccountBacktester(config=self._backtest_config())
        backtester.provider = provider
        with patch.object(provider, "fetch_historical_posts", return_value=posts):
            result = backtester.run("CryptoCapo_", days=60)

        self.assertEqual(len(result.signals), 1)
        self.assertFalse(result.signals[0].was_correct_7d)

    def test_telegram_summary_includes_evaluation_modes(self):
        from intelligence.x_account_backtest import BacktestResult, BacktestSignal

        result = BacktestResult(handle="Trader", days=30, tweets_fetched=5)
        result.signals = [
            BacktestSignal(
                post_id="1", timestamp=datetime.now(timezone.utc), coin="BTC", action="BUY",
                confidence=80, rationale="x", raw_tweet="t", signal_price=100, exit_price=104,
                return_24h=4, was_correct=True, return_7d=8, was_correct_7d=True,
                has_price_target=True, target_hit=True, bot_would_trade=True, bot_was_correct_7d=True,
            ),
        ]
        summary = result.to_telegram_summary()
        self.assertIn("7d (Swing)", summary)
        self.assertIn("Tweet-Ziel", summary)
        self.assertIn("Bot-Strategie", summary)

    def test_mock_fetch_historical_posts_respects_days(self):
        provider = MockXProvider()
        posts = provider.fetch_historical_posts("CryptoCapo_", days=30, max_posts=50)
        self.assertGreater(len(posts), 0)
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        for post in posts:
            created = datetime.fromisoformat(post.created_at.replace("Z", "+00:00"))
            self.assertGreaterEqual(created, cutoff)

    @patch("notifications.telegram_commands.x_commands.send_telegram_message")
    def test_testaccount_bare_command_shows_hint(self, mock_send):
        handled = handle("/testaccount")
        self.assertTrue(handled)
        mock_send.assert_called_once()

    @patch("notifications.telegram_commands.x_commands.heavy_job_queue.enqueue")
    @patch("notifications.telegram_commands.x_commands.send_telegram_message")
    def test_testaccount_starts_background_job(self, mock_send, mock_enqueue):
        mock_enqueue.return_value = ("job123", None)
        handled = handle("/testaccount CryptoCapo_ 45")
        self.assertTrue(handled)
        mock_enqueue.assert_called_once()
        mock_send.assert_called_once()
        self.assertIn("45", mock_send.call_args[0][0])

    @patch("notifications.telegram_commands.x_commands.save_x_accounts")
    @patch("notifications.telegram_commands.x_commands.load_x_accounts")
    @patch("notifications.telegram_commands.x_commands.answer_callback_query")
    @patch("notifications.telegram_commands.x_commands.send_telegram_message")
    def test_callback_add_account(self, mock_send, mock_answer, mock_load, mock_save):
        mock_load.return_value = []
        mock_save.return_value = True
        handled = handle_callback({
            "id": "cb1",
            "data": "testaccount_add:NewTrader",
        })
        self.assertTrue(handled)
        mock_answer.assert_called_once_with("cb1")
        mock_save.assert_called_once()
        mock_send.assert_called_once()
        self.assertIn("Added", mock_send.call_args[0][0])

    @patch("notifications.telegram_commands.x_commands.load_x_accounts")
    @patch("notifications.telegram_commands.x_commands.save_x_accounts")
    def test_add_x_account_already_exists(self, mock_save, mock_load):
        mock_load.return_value = [{"handle": "CryptoCapo_", "trust_score": 70}]
        ok, msg = add_x_account("CryptoCapo_")
        self.assertFalse(ok)
        mock_save.assert_not_called()
        self.assertIn("already", msg)


if __name__ == "__main__":
    unittest.main()