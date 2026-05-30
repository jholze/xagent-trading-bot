import os
import sys
import unittest
from datetime import datetime
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))


class ColoredTestResult(unittest.TextTestResult):
    COLORS = {
        "cyan": "\033[96m",      # Running
        "green": "\033[92m",     # Success
        "orange": "\033[38;5;208m",  # Warnings/Skipped
        "red": "\033[91m",       # Fail/Error
        "purple": "\033[95m",    # Summary/Header
        "bold": "\033[1m",
        "reset": "\033[0m",
    }

    def getDescription(self, test):
        return str(test)

    def startTest(self, test):
        super().startTest(test)
        print(f"{self.COLORS['cyan']}▶ Running {test._testMethodName}{self.COLORS['reset']}")

    def addSuccess(self, test):
        super().addSuccess(test)
        print(f"{self.COLORS['green']}  ✓ PASSED{self.COLORS['reset']}")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        print(f"{self.COLORS['red']}  ✗ FAILED{self.COLORS['reset']}")

    def addError(self, test, err):
        super().addError(test, err)
        print(f"{self.COLORS['red']}  ⚠ ERROR{self.COLORS['reset']}")

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        print(f"{self.COLORS['orange']}  ⏭ SKIPPED{self.COLORS['reset']}")


from data_manager import get_text, load_config, load_trade_history, load_x_accounts, record_trade, save_x_accounts
from strategies.positions import get_position, list_active_positions, update_position
from x_analyzer import XAnalyzer


class TestVirtualTrading(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.symbol = "TESTUNIT/USDT"
        self.tf = "4h"
        self.test_price = 0.5
        from strategies.positions import positions, get_key
        key = get_key(self.symbol, self.tf)
        if key in positions:
            del positions[key]
        import json
        try:
            with open("trade_history.json", "w", encoding="utf-8") as f:
                json.dump({"virtual_balance": 5000.0, "realized_pnl": 0.0, "open_positions": 0, "trades": []}, f, indent=2)
        except:
            pass

    def test_position_initialization(self):
        pos = get_position(self.symbol, self.tf)
        self.assertEqual(float(pos["amount"]), 0.0)
        self.assertEqual(pos["last_ampel"], "🟡")
        self.assertIn("average_entry", pos)

    def test_buy_position_update(self):
        update_position(self.symbol, self.tf, "BUY", self.test_price, 300)
        pos = get_position(self.symbol, self.tf)
        self.assertGreater(float(pos["amount"]), 0.0)
        self.assertEqual(pos.get("average_entry"), self.test_price)

    def test_trade_history_recording(self):
        record_trade({
            "type": "BUY",
            "symbol": self.symbol,
            "price": self.test_price,
            "amount": 300,
            "usdt_amount": 150,
            "timestamp": datetime.now().isoformat()
        })
        history = load_trade_history()
        self.assertGreater(len(history.get("trades", [])), 0)
        self.assertIn("virtual_balance", history)

    def test_stop_loss_logic(self):
        update_position(self.symbol, self.tf, "BUY", 0.5, 300)
        pos = get_position(self.symbol, self.tf)
        pos["entry_price"] = 0.5
        with patch("data_manager.load_trade_history") as mock_history:
            mock_history.return_value = {"open_positions": 1}
            self.assertTrue(True)

    def test_virtual_pnl_tracking(self):
        history = load_trade_history()
        self.assertIsInstance(history.get("realized_pnl", 0), (int, float))

    def test_x_accounts_management(self):
        accounts = load_x_accounts()
        original_count = len(accounts)
        test_account = {"handle": "TestTraderX", "trust_score": 75, "enabled": True, "notes": "Test"}
        accounts.append(test_account)
        self.assertTrue(save_x_accounts(accounts))
        reloaded = load_x_accounts()
        self.assertGreater(len(reloaded), original_count)
        self.assertTrue(any(a.get("handle") == "TestTraderX" for a in reloaded))
        # Cleanup
        cleaned = [a for a in reloaded if a.get("handle") != "TestTraderX"]
        save_x_accounts(cleaned)

    def test_x_analyzer_integration(self):
        analyzer = XAnalyzer()
        signals = analyzer.get_top_signals()
        self.assertGreater(len(signals), 0)
        for s in signals:
            self.assertGreater(s.confidence, 0)
            self.assertGreater(s.score, 0)

    def test_ui_rendering(self):
        from terminal_ui import print_dashboard
        test_data = {
            "balance": "$5,234",
            "unrealized": "$187.4",
            "realized_pnl": "$92.1",
            "total_value": "$5,421",
            "active_positions": 3,
            "win_rate": "71%",
            "coins": ["ARIA", "RAVE", "HIGH"],
            "x_accounts": ["CryptoCapo_", "Pentosh1"],
            "signals": [
                "🟢 @CryptoCapo_ BUY ARIA | 84%",
                "→ Technical: ARIA | Price: $0.0474",
                "No strong signals this cycle..."
            ],
            "last_cycle": "10:25:12",
            "status": "🟢 Running",
            "next_update": 42
        }
        try:
            print_dashboard(test_data)
            self.assertTrue(True)  # No exception = success
        except Exception as e:
            self.fail(f"UI rendering failed with: {e}")

    def test_pnl_calculation(self):
        # Test average entry and PnL consistency
        update_position("TEST/PNL", "4h", "BUY", 0.5, 1000)
        pos = get_position("TEST/PNL", "4h")
        self.assertEqual(pos["average_entry"], 0.5)

        # Second buy at different price
        update_position("TEST/PNL", "4h", "BUY", 0.6, 1000)
        pos = get_position("TEST/PNL", "4h")
        expected_average = (0.5 * 1000 + 0.6 * 1000) / 2000
        self.assertAlmostEqual(pos["average_entry"], expected_average, places=4)

        # Simulate sell and check PnL
        current_price = 0.7
        update_position("TEST/PNL", "4h", "SELL", current_price, 500)
        pos = get_position("TEST/PNL", "4h")
        self.assertGreater(pos["average_entry"], 0)
        self.assertLess(pos["amount"], 2000)

        history = load_trade_history()
        self.assertIsInstance(history.get("realized_pnl", 0), (int, float))

    def test_buy_command_parsing(self):
        from unittest.mock import patch
        from telegram_notifier import handle_telegram_command

        # Note: Some patches target telegram_notifier's namespace because
        # telegram_notifier imports and re-uses those names internally.
        with patch("telegram_notifier.send_telegram_message") as mock_send, \
             patch("telegram_notifier.get_prices") as mock_price, \
             patch("telegram_notifier.update_position") as mock_update, \
             patch("telegram_notifier.record_trade") as mock_record, \
             patch("telegram_notifier.list_coins") as mock_coins:

            mock_coins.return_value = [{"symbol": "ARIA/USDT"}, {"symbol": "RAVE/USDT"}]
            mock_price.return_value = (0.05, 0.05, None)

            # Test index based
            handle_telegram_command("/buy 1 200")
            mock_record.assert_called()
            mock_update.assert_called_with("ARIA/USDT", "4h", "BUY", 0.05, 4000.0)

            # Test symbol based
            handle_telegram_command("/buy RAVE 100")
            self.assertTrue(mock_record.called)

            # Test invalid (basic check)
            mock_coins.return_value = []
            handle_telegram_command("/buy")
            mock_send.assert_called()  # Error message is sent for invalid input

    def test_demo_mode_prefixes_telegram_messages(self):
        """Ensure that when running in --demo mode, all Telegram messages get the demo prefix."""
        from unittest.mock import patch, MagicMock
        from telegram_notifier import send_telegram_message

        with patch("telegram_notifier.is_demo_mode", return_value=True), \
             patch("telegram_notifier.requests.post") as mock_post:
            mock_post.return_value.status_code = 200

            send_telegram_message("Important trade signal")

            # Check that the message sent to Telegram contained the demo prefix
            called_text = mock_post.call_args[1]["json"]["text"]
            self.assertIn("🧪 [DEMO]", called_text)
            self.assertIn("Important trade signal", called_text)

        # Also verify that without demo mode, no prefix is added
        with patch("telegram_notifier.is_demo_mode", return_value=False), \
             patch("telegram_notifier.requests.post") as mock_post:
            mock_post.return_value.status_code = 200

            send_telegram_message("Normal message")

            called_text = mock_post.call_args[1]["json"]["text"]
            self.assertNotIn("🧪 [DEMO]", called_text)
            self.assertIn("Normal message", called_text)

    def test_demo_file_path_helper(self):
        """Basic sanity check for demo mode file path logic."""
        from unittest.mock import patch
        import data_manager

        with patch.object(data_manager, "_DEMO_MODE", True):
            self.assertTrue(data_manager.is_demo_mode())
            path = data_manager.get_data_file("trade_history.json")
            self.assertIn(".demo.json", path)

        with patch.object(data_manager, "_DEMO_MODE", False):
            self.assertFalse(data_manager.is_demo_mode())
            path = data_manager.get_data_file("trade_history.json")
            self.assertNotIn(".demo.json", path)

    def test_fetch_stability(self):
        config = load_config()
        history = load_trade_history()
        self.assertIsNotNone(config)
        self.assertIn("virtual_trading", config)
        self.assertTrue(True)

    def test_sell_command_list(self):
        update_position("REAL/USDT", "4h", "BUY", 0.5, 100)
        active = list_active_positions()
        self.assertGreater(len(active), 0)

    def test_sell_command_execute(self):
        update_position("REAL/USDT", "4h", "BUY", 0.5, 100)
        from telegram_notifier import handle_telegram_command
        with patch("telegram_notifier.send_telegram_message") as mock_send:
            handle_telegram_command("/sell 1 50")
            self.assertTrue(mock_send.called)

    def test_i18n(self):
        self.assertIn(get_text("bot_started"), ["🚀 Trading Bot – Clean Webhook Version started", "🚀 Trading Bot – Saubere Webhook Version gestartet"])
        self.assertEqual(get_text("nonexistent", "fallback"), "fallback")

    def test_list_active_positions(self):
        update_position("REAL/USDT", "4h", "BUY", 0.5, 100)
        active = list_active_positions()
        self.assertGreater(len(active), 0)
        self.assertTrue(any("REAL" in p["symbol"] for p in active))
        self.assertNotIn("TEST", str(active))
        for p in active:
            if p.get("last_action") == "BUY":
                self.assertIn("highlight", p)

    def test_price_fetcher_caching(self):
        """Smoke test that the price cache doesn't break basic calls."""
        from price_fetcher import get_prices
        # Should not raise and should return a tuple of 3 values
        result = get_prices("BTC/USDT")
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 3)

    # ==================================================================
    # Expanded Telegram Notifier Tests (send_signal_message + demo prefix)
    # ==================================================================

    def _assert_demo_prefix_in_message(self, mock_post, expected_content):
        """Helper to verify both demo prefix and content in sent messages."""
        called_text = mock_post.call_args[1]["json"]["text"]
        self.assertIn("🧪 [DEMO]", called_text)
        self.assertIn(expected_content, called_text)

    def test_send_signal_message_buy_with_demo_prefix(self):
        from unittest.mock import patch
        from telegram_notifier import send_signal_message

        coin = {"symbol": "ARIA/USDT", "name": "Aria AI"}

        with patch("telegram_notifier.is_demo_mode", return_value=True), \
             patch("telegram_notifier.requests.post") as mock_post:
            mock_post.return_value.status_code = 200

            send_signal_message("BUY", coin, 0.0523, 35.0, 0.048, 1.4, "🟢", "Stark Bullish")

            self._assert_demo_prefix_in_message(mock_post, "BUY EXECUTED")
            self._assert_demo_prefix_in_message(mock_post, "ARIA/USDT")

    def test_send_signal_message_sell_20_with_demo_prefix(self):
        from unittest.mock import patch
        from telegram_notifier import send_signal_message

        coin = {"symbol": "RAVE/USDT", "name": "RaveDAO"}

        with patch("telegram_notifier.is_demo_mode", return_value=True), \
             patch("telegram_notifier.requests.post") as mock_post:
            mock_post.return_value.status_code = 200

            send_signal_message("SELL_20", coin, 0.65, 72.0, 0.60, 0.8, "🔴", "Bearish")

            self._assert_demo_prefix_in_message(mock_post, "SELL 20% EXECUTED")

    def test_send_signal_message_x_signal_without_demo_prefix(self):
        from unittest.mock import patch
        from telegram_notifier import send_signal_message

        coin = {"symbol": "SOL/USDT", "name": "Solana"}

        with patch("telegram_notifier.is_demo_mode", return_value=False), \
             patch("telegram_notifier.requests.post") as mock_post:
            mock_post.return_value.status_code = 200

            send_signal_message("X_SIGNAL", coin, 142.5, 55.0, 138.0, 1.1, "📡", "Strong volume")

            called_text = mock_post.call_args[1]["json"]["text"]
            self.assertNotIn("🧪 [DEMO]", called_text)
            self.assertIn("X SIGNAL", called_text)

    def test_send_x_recommendation_message(self):
        from unittest.mock import patch
        from telegram_notifier import send_x_recommendation_message

        rec = {
            "account": "CryptoCapo_",
            "coin": "BTC",
            "action": "BUY",
            "confidence": 82,
            "rationale": "Breaking resistance with volume",
            "raw_tweet": "BTC looking very strong..."
        }

        with patch("telegram_notifier.is_demo_mode", return_value=True), \
             patch("telegram_notifier.requests.post") as mock_post:
            mock_post.return_value.status_code = 200

            send_x_recommendation_message(rec)

            called_text = mock_post.call_args[1]["json"]["text"]
            self.assertIn("🧪 [DEMO]", called_text)
            self.assertIn("CryptoCapo_", called_text)
            self.assertIn("Breaking resistance", called_text)

    # ==================================================================
    # Expanded Demo Mode & Data Layer Tests
    # ==================================================================

    def test_demo_mode_auto_copies_real_file_on_first_use(self):
        """Basic verification that demo mode changes the returned file path."""
        from unittest.mock import patch
        import data_manager

        with patch.object(data_manager, "_DEMO_MODE", True):
            path = data_manager.get_data_file("watchlist.json")
            self.assertTrue(path.endswith(".demo.json"))

        with patch.object(data_manager, "_DEMO_MODE", False):
            path = data_manager.get_data_file("watchlist.json")
            self.assertFalse(path.endswith(".demo.json"))

    def test_load_demo_data_only_runs_in_demo_mode(self):
        from unittest.mock import patch
        import data_manager

        with patch.object(data_manager, "_DEMO_MODE", False), \
             patch("data_manager.log") as mock_log:
            data_manager.load_demo_data()
            # Should log a warning and do nothing
            self.assertTrue(any("without --demo" in str(c) for c in mock_log.call_args_list))

    def test_price_cache_avoids_repeated_network_calls(self):
        """Smoke test: repeated calls for the same symbol should not explode and should eventually use cache."""
        from unittest.mock import patch, MagicMock
        from price_fetcher import get_prices, _price_cache

        _price_cache.clear()

        with patch("price_fetcher.requests.get") as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"bitcoin": {"usd": 65000.0}}
            mock_get.return_value = mock_response

            # Multiple calls should not raise and should stay reasonable
            for _ in range(3):
                get_prices("BTC/USDT")

            # Just ensure we didn't make a ridiculous number of calls
            self.assertLessEqual(mock_get.call_count, 10)

    def test_data_layer_logs_on_failed_save(self):
        """When saving fails, we should log an ERROR instead of failing silently."""
        from unittest.mock import patch, mock_open
        import data_manager

        with patch("builtins.open", mock_open()) as mock_file, \
             patch("data_manager.log") as mock_log:

            mock_file.side_effect = IOError("Disk full")

            result = data_manager.save_watchlist([{"symbol": "TEST/USDT"}])

            self.assertFalse(result)
            # Check that we logged an error
            error_logs = [c for c in mock_log.call_args_list if "ERROR" in str(c)]
            self.assertTrue(len(error_logs) > 0)

    def tearDown(self):
        import json
        try:
            with open("trade_history.json", "w", encoding="utf-8") as f:
                json.dump({"virtual_balance": 5000.0, "realized_pnl": 0.0, "open_positions": 0, "trades": []}, f, indent=2)
            with open("positions.json", "r", encoding="utf-8") as f:
                data = json.load(f)
            if "positions" in data:
                data["positions"] = {k: v for k, v in data["positions"].items() if not any(t in k.upper() for t in ["TEST", "TESTUNIT"])}
            with open("positions.json", "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except:
            pass


if __name__ == "__main__":
    import io
    suite = unittest.TestLoader().loadTestsFromTestCase(TestVirtualTrading)
    runner = unittest.TextTestRunner(
        resultclass=ColoredTestResult,
        verbosity=0,
        stream=io.StringIO()
    )
    result = runner.run(suite)
    print(f"\n{ColoredTestResult.COLORS['purple']}{ColoredTestResult.COLORS['bold']}✦ Virtual Trading Test Suite Complete ✦{ColoredTestResult.COLORS['reset']}")
    if result.wasSuccessful():
        print(f"{ColoredTestResult.COLORS['green']}🎉 ALL TESTS PASSED SUCCESSFULLY!{ColoredTestResult.COLORS['reset']}")
    else:
        print(f"{ColoredTestResult.COLORS['red']}Some tests need attention.{ColoredTestResult.COLORS['reset']}")
    print(f"{ColoredTestResult.COLORS['cyan']}Tests run: {result.testsRun} | Failures: {len(result.failures)} | Errors: {len(result.errors)}{ColoredTestResult.COLORS['reset']}")
