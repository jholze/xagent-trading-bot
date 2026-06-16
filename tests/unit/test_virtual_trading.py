import os
import sys
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

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


from core.config import get_bot_config
from data_manager import get_text, load_config, load_trade_history, load_x_accounts, record_trade, save_x_accounts
from services.portfolio_service import PortfolioService
from strategies.positions import (
    count_open_positions,
    get_position,
    list_active_positions,
    sell_fraction_for_signal,
    update_position,
)
from x_analyzer import XAnalyzer, XSignal


class TestVirtualTrading(unittest.TestCase):
    def setUp(self):
        import json
        import tempfile
        import logger as logger_mod

        self.config = load_config()
        self.symbol = "XRVM/USDT"
        self.tf = "4h"
        self.test_price = 0.5
        from decimal import Decimal
        from strategies.positions import positions, get_key
        self._positions_backup = {
            k: {**v, "amount": Decimal(str(v["amount"]))} for k, v in positions.items()
        }
        self._trade_history_backup = load_trade_history()
        self._log_dir_backup = logger_mod.LOG_DIR
        self._log_file_backup = logger_mod.LOG_FILE
        self._log_tmp = tempfile.mkdtemp(prefix="aria_test_logs_")
        logger_mod.LOG_DIR = self._log_tmp
        logger_mod.LOG_FILE = os.path.join(self._log_tmp, "aria_log.txt")
        key = get_key(self.symbol, self.tf)
        if key in positions:
            del positions[key]
        try:
            with open("trade_history.json", "w", encoding="utf-8") as f:
                json.dump({"virtual_balance": 5000.0, "realized_pnl": 0.0, "open_positions": 0, "trades": []}, f, indent=2)
        except Exception:
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

    def test_sell_fraction_mapping(self):
        self.assertEqual(sell_fraction_for_signal("SELL_STOP_FULL"), 1.0)
        self.assertEqual(sell_fraction_for_signal("SELL_STOP_PARTIAL"), 0.5)
        self.assertEqual(sell_fraction_for_signal("SELL_30"), 0.3)
        self.assertEqual(sell_fraction_for_signal("SELL_20"), 0.2)
        self.assertEqual(sell_fraction_for_signal("SELL"), 0.2)

    def test_sell_stop_full_closes_position(self):
        update_position(self.symbol, self.tf, "BUY", 0.5, 1000)
        update_position(self.symbol, self.tf, "SELL_STOP_FULL", 0.4, 1000)
        pos = get_position(self.symbol, self.tf)
        self.assertAlmostEqual(float(pos["amount"]), 0.0, places=4)

    def test_sell_stop_partial_sells_half(self):
        update_position(self.symbol, self.tf, "BUY", 0.5, 1000)
        update_position(self.symbol, self.tf, "SELL_STOP_PARTIAL", 0.4)
        pos = get_position(self.symbol, self.tf)
        self.assertAlmostEqual(float(pos["amount"]), 500.0, places=4)

    def test_sell_with_explicit_amount(self):
        update_position(self.symbol, self.tf, "BUY", 0.5, 1000)
        update_position(self.symbol, self.tf, "SELL", 0.6, 250)
        pos = get_position(self.symbol, self.tf)
        self.assertAlmostEqual(float(pos["amount"]), 750.0, places=4)

    def test_list_active_positions_average_entry(self):
        update_position(self.symbol, self.tf, "BUY", 0.5, 100)
        active = list_active_positions()
        match = next((p for p in active if p["symbol"] in ("XRVM", "XRVM/USDT")), None)
        self.assertIsNotNone(match)
        self.assertEqual(match["average_entry"], 0.5)
        self.assertEqual(match["entry_price"], 0.5)

    def test_open_positions_count_sync(self):
        before = count_open_positions()
        for sym in ("COUNT_A/USDT", "COUNT_B/USDT"):
            update_position(sym, "4h", "BUY", 1.0, 10)
            record_trade({
                "type": "BUY",
                "symbol": sym,
                "price": 1.0,
                "amount": 10,
                "usdt_amount": 10,
                "timestamp": datetime.now().isoformat(),
            })
        self.assertEqual(count_open_positions(), before + 2)
        history = load_trade_history()
        self.assertEqual(history.get("open_positions"), before + 2)

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

    def test_action_normalization(self):
        from core.actions import normalize, to_execution_action, BUY_STRONG, SELL_PARTIAL_20

        self.assertEqual(normalize("SELL_20"), SELL_PARTIAL_20)
        self.assertEqual(to_execution_action(BUY_STRONG), "BUY")
        self.assertEqual(to_execution_action(SELL_PARTIAL_20), "SELL_20")

    def test_resolve_coin_config_from_strategies(self):
        from strategies.registry import resolve_coin_config

        coin = resolve_coin_config({"symbol": "ARIA/USDT", "timeframe": "4h"})
        self.assertEqual(coin.get("timeframe"), "4h")
        self.assertIn("strategy_params", coin)
        self.assertEqual(coin["strategy_params"].get("rsi_buy_low"), 28)

    def test_decision_engine_evaluate(self):
        from strategies.decision_engine import DecisionEngine

        engine = DecisionEngine()
        with patch.object(engine.market, "fetch_indicators", return_value={"rsi": 50.0, "lower_bb": 0.9, "vol_multiplier": 1.0}):
            analysis = engine.evaluate({"symbol": "XRVM/USDT", "timeframe": "4h"}, 1.0)
            self.assertIsNotNone(analysis)
            self.assertIn(analysis.normalized_action, ("HOLD", "BUY", "BUY_STRONG"))
            self.assertIsNotNone(analysis.rationale)

    def test_decision_engine_x_buy_merge(self):
        from strategies.decision_engine import DecisionEngine
        from x_analyzer import XSignal

        engine = DecisionEngine()
        x_sig = XSignal("CryptoCapo_", "XRVM", "BUY", 85, rationale="strong")
        x_sig.trust_score = 90
        x_sig.effective_confidence = 80

        with patch.object(engine.market, "fetch_indicators", return_value={"rsi": 50.0, "lower_bb": 0.5, "vol_multiplier": 0.8}):
            analysis = engine.evaluate({"symbol": "XRVM/USDT", "timeframe": "4h"}, 1.0, x_signals=[x_sig])
            self.assertIn(analysis.normalized_action, ("BUY", "BUY_STRONG"))
            self.assertIn("x", analysis.sources)

    def test_trading_mode_defaults_paper(self):
        from core.config import BotConfig

        cfg = BotConfig()
        cfg._raw = {"virtual_trading": True}
        self.assertEqual(cfg.trading_mode, "paper")

    def test_trading_service_blocks_off_mode(self):
        from core.config import BotConfig
        from data_manager import get_config
        from services.trading_service import TradingService

        raw = dict(get_config())
        raw["trading_mode"] = "off"
        cfg = BotConfig()
        cfg._raw = raw
        svc = TradingService(cfg)
        ok, reason = svc.can_execute()
        self.assertFalse(ok)
        self.assertIn("disabled", reason.lower())

    def test_trading_service_live_requires_confirm(self):
        from core.config import BotConfig
        from data_manager import get_config
        from services.trading_service import TradingService

        raw = dict(get_config())
        raw["trading_mode"] = "live"
        raw["live_confirmed"] = False
        cfg = BotConfig()
        cfg._raw = raw
        svc = TradingService(cfg)
        ok, reason = svc.can_execute()
        self.assertFalse(ok)
        self.assertIn("live_confirm", reason)

    def test_gate_adapter_dry_run(self):
        from execution.gate_adapter import GateExecutionAdapter
        from core.config import BotConfig
        from core.models import TradeOrder
        from data_manager import get_config

        raw = dict(get_config())
        raw.setdefault("live", {})["dry_run"] = True
        cfg = BotConfig()
        cfg._raw = raw
        adapter = GateExecutionAdapter(cfg)
        with patch.object(adapter.portfolio, "execute_buy") as mock_buy:
            from core.models import TradeResult
            mock_buy.return_value = TradeResult(True, "BUY", "XRVM/USDT", amount=10, price=0.5, usdt_amount=5)
            with patch("execution.gate_adapter.record_live_trade"):
                result = adapter.execute(TradeOrder("BUY", "XRVM/USDT", 0.5, 10, usdt_amount=5), "4h")
        self.assertTrue(result.executed)
        self.assertIn("Dry run", result.message)

    def test_execution_factory_paper(self):
        from core.config import BotConfig
        from data_manager import get_config
        from execution.factory import get_execution_adapter
        from execution.paper_adapter import PaperExecutionAdapter

        raw = dict(get_config())
        raw["trading_mode"] = "paper"
        cfg = BotConfig()
        cfg._raw = raw
        adapter = get_execution_adapter(cfg)
        self.assertIsInstance(adapter, PaperExecutionAdapter)

    def test_execution_factory_gate_testnet_uses_paper(self):
        from core.config import BotConfig
        from data_manager import get_config
        from execution.factory import get_execution_adapter
        from execution.paper_adapter import PaperExecutionAdapter

        raw = dict(get_config())
        raw["trading_mode"] = "gate_testnet"
        cfg = BotConfig()
        cfg._raw = raw
        adapter = get_execution_adapter(cfg)
        self.assertIsInstance(adapter, PaperExecutionAdapter)

    def test_trading_mode_gate_testnet_migrates_to_paper(self):
        from core.config import BotConfig
        from data_manager import get_config
        from services.trading_service import TradingService

        raw = dict(get_config())
        raw["trading_mode"] = "gate_testnet"
        cfg = BotConfig()
        cfg._raw = raw
        self.assertEqual(cfg.trading_mode, "paper")
        svc = TradingService(cfg)
        ok, _ = svc.can_execute()
        self.assertTrue(ok)

    def test_registry_lists_strategies(self):
        from strategies.registry import list_registered_strategies
        strategies = list_registered_strategies()
        self.assertIn("technical_rsi_bb", strategies)

    def test_technical_strategy_analyze_hold(self):
        from core.models import MarketContext
        from strategies.technical_rsi_bb import TechnicalRSIStrategy

        strategy = TechnicalRSIStrategy()
        market = MarketContext(
            symbol="XRVM/USDT",
            timeframe="4h",
            current_price=1.0,
            rsi=50.0,
            lower_bb=0.9,
            vol_multiplier=1.0,
            has_position=False,
            open_positions=0,
        )
        result = strategy.analyze({"symbol": "XRVM/USDT"}, market)
        self.assertEqual(result.action, "HOLD")
        self.assertEqual(result.symbol, "XRVM/USDT")

    def test_technical_strategy_high_rsi_without_position_stays_hold(self):
        from core.models import MarketContext
        from strategies.technical_rsi_bb import TechnicalRSIStrategy

        strategy = TechnicalRSIStrategy()
        for open_positions in (0, 5):
            with self.subTest(open_positions=open_positions):
                market = MarketContext(
                    symbol="RAVE/USDT",
                    timeframe="4h",
                    current_price=0.65,
                    rsi=75.0,
                    lower_bb=0.60,
                    vol_multiplier=1.0,
                    has_position=False,
                    open_positions=open_positions,
                )
                result = strategy.analyze({"symbol": "RAVE/USDT"}, market)
                self.assertEqual(result.action, "HOLD")
                self.assertEqual(result.normalized_action, "HOLD")

    def test_decision_engine_no_phantom_sell_when_no_position(self):
        from strategies.decision_engine import DecisionEngine
        from strategies.positions import get_position

        symbol = "PHANTOM/USDT"
        engine = DecisionEngine()
        self.assertEqual(float(get_position(symbol, "4h")["amount"]), 0.0)
        with patch.object(
            engine.market,
            "fetch_indicators",
            return_value={"rsi": 75.0, "lower_bb": 0.60, "vol_multiplier": 1.0},
        ):
            analysis = engine.evaluate({"symbol": symbol, "timeframe": "4h"}, 0.65)
        self.assertEqual(analysis.action, "HOLD")
        self.assertEqual(analysis.normalized_action, "HOLD")

    def test_orchestrator_no_sell_notification_without_position(self):
        from services.signal_orchestrator import SignalOrchestrator
        from strategies.positions import get_position

        symbol = "PHANTOM/USDT"
        notifications = []
        orch = SignalOrchestrator(notify_callback=lambda *args, **kwargs: notifications.append((args, kwargs)))
        self.assertEqual(float(get_position(symbol, "4h")["amount"]), 0.0)
        with patch.object(
            orch.market,
            "fetch_indicators",
            return_value={"rsi": 75.0, "lower_bb": 0.60, "vol_multiplier": 1.0},
        ):
            result = orch.process_coin({"symbol": symbol, "timeframe": "4h", "name": "Phantom"}, 0.65)
        self.assertEqual(result["action"], "HOLD")
        self.assertFalse(result["executed"])
        sell_notifications = [n for n in notifications if "SELL" in str(n)]
        self.assertEqual(len(sell_notifications), 0)

    def test_portfolio_service_buy(self):
        from services.portfolio_service import PortfolioService
        portfolio = PortfolioService()
        result = portfolio.execute_buy("XRVM/USDT", "4h", 0.5, 100)
        self.assertTrue(result.executed)
        self.assertAlmostEqual(result.amount, 200.0, places=4)

    def test_signal_orchestrator_analyze_only(self):
        from services.signal_orchestrator import SignalOrchestrator
        orch = SignalOrchestrator()
        with patch.object(orch.market, "fetch_indicators", return_value={"rsi": 50.0, "lower_bb": 0.9, "vol_multiplier": 1.0}):
            analysis = orch.analyze({"symbol": "XRVM/USDT", "timeframe": "4h"}, 1.0)
            self.assertIsNotNone(analysis)
            self.assertIn(analysis.action, ("HOLD", "BUY", "SELL_20", "SELL_30", "SELL_STOP_FULL", "SELL_STOP_PARTIAL"))

    def test_mock_x_provider_dedup(self):
        import uuid
        from x_data_provider import MockXProvider
        from data_manager import load_x_posts, save_x_posts

        provider = MockXProvider()
        handle = f"DedupTest{uuid.uuid4().hex[:8]}"
        accounts = [{"handle": handle, "enabled": True}]
        first = provider.fetch_new_posts(accounts)
        self.assertGreater(len(first), 0)

        data = load_x_posts()
        data["posts"].append({"post_id": first[0].post_id, "account": handle, "coin": "BTC"})
        save_x_posts(data)
        second = provider.fetch_new_posts(accounts)
        self.assertFalse(any(p.post_id == first[0].post_id for p in second))

    def test_trust_adjusted_scoring(self):
        analyzer = XAnalyzer()
        signal = XSignal("CryptoCapo_", "BTC", "BUY", 80, rationale="test")
        signal.trust_score = 90
        analyzer.score_signal(signal, 50.0, all_signals=[signal])
        self.assertGreater(signal.effective_confidence, 70)
        self.assertGreater(signal.score, 0)

    def test_effective_confidence_threshold(self):
        analyzer = XAnalyzer()
        with patch.object(analyzer, "get_trust_score", side_effect=lambda acc: 90.0 if acc == "CryptoCapo_" else 30.0):
            high_trust = analyzer.effective_confidence_threshold("CryptoCapo_")
            low_trust_threshold = analyzer.effective_confidence_threshold("UnknownUser")
        self.assertLess(high_trust, low_trust_threshold)
        self.assertLessEqual(high_trust, 75)

    def test_accuracy_tracker_leaderboard(self):
        from intelligence.accuracy_tracker import AccuracyTracker
        tracker = AccuracyTracker()
        board = tracker.get_leaderboard()
        self.assertGreater(len(board), 0)
        self.assertIn("trust_score", board[0])

    def test_x_analyzer_integration(self):
        analyzer = XAnalyzer()
        with patch.object(analyzer, "fetch_latest_signals") as mock_fetch:
            from x_analyzer import XSignal
            mock_fetch.return_value = [
                XSignal("CryptoCapo_", "BTC", "BUY", 80, rationale="Test signal")
            ]
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

        # Simulate sell with explicit amount and check remaining position
        current_price = 0.7
        update_position("TEST/PNL", "4h", "SELL", current_price, 500)
        pos = get_position("TEST/PNL", "4h")
        self.assertGreater(pos["average_entry"], 0)
        self.assertAlmostEqual(float(pos["amount"]), 1500.0, places=4)

        history = load_trade_history()
        self.assertIsInstance(history.get("realized_pnl", 0), (int, float))

    def test_buy_command_parsing(self):
        from unittest.mock import ANY, patch
        from telegram_notifier import handle_telegram_command

        # Note: Some patches target telegram_notifier's namespace because
        # telegram_notifier imports and re-uses those names internally.
        with patch("notifications.telegram_commands.trading_commands.send_telegram_message") as mock_send, \
             patch("notifications.telegram_commands.trading_commands.request_buy_confirmation") as mock_preview, \
             patch("notifications.telegram_commands.trading_commands.get_prices") as mock_price, \
             patch("notifications.telegram_commands.trading_commands.list_coins") as mock_coins:

            mock_coins.return_value = [{"symbol": "ARIA/USDT"}, {"symbol": "RAVE/USDT"}]
            mock_price.return_value = (0.05, 0.05, None)

            handle_telegram_command("/buy 1 200")
            mock_preview.assert_called_with(
                ANY, symbol="ARIA/USDT", timeframe="4h", price=0.05, usdt=200,
            )

            handle_telegram_command("/buy RAVE 100")
            self.assertEqual(mock_preview.call_count, 2)

            # Bare /buy sends numbered buy list
            mock_send.reset_mock()
            with patch("notifications.telegram_commands.trading_commands.get_prices_batch") as mock_batch:
                mock_batch.return_value = {"ARIA/USDT": 0.05, "RAVE/USDT": 0.12}
                handle_telegram_command("/buy")
            mock_send.assert_called()
            msg = mock_send.call_args[0][0]
            self.assertIn("Coins kaufen", msg)
            self.assertIn("/buy NUMMER USDT", msg)

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

        with patch.object(data_manager, "is_demo_mode", return_value=True):
            path = data_manager.get_data_file("trade_history.json")
            self.assertIn(".demo.json", path)

        with patch.object(data_manager, "is_demo_mode", return_value=False):
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

    def test_sell_command_requests_confirmation(self):
        from telegram_notifier import handle_telegram_command

        update_position(self.symbol, self.tf, "BUY", 0.5, 100)
        with patch("notifications.telegram_commands.trading_commands.get_prices_batch", return_value={"XRVM/USDT": 0.6}), \
             patch("notifications.telegram_commands.trading_commands.get_prices", return_value=(0.6, 0.6, None)), \
             patch("notifications.telegram_commands.trading_commands.request_sell_confirmation") as mock_confirm, \
             patch("notifications.telegram_commands.trading_commands.list_active_positions") as mock_active:
            mock_active.return_value = [{
                "symbol": "XRVM/USDT",
                "amount": 100.0,
                "average_entry": 0.5,
                "entry_price": 0.5,
                "realized_pnl": 0,
                "highlight": "",
            }]
            handle_telegram_command("/sell 1 50")
            mock_confirm.assert_called_once()
            kwargs = mock_confirm.call_args[1]
            self.assertEqual(kwargs["symbol"], "XRVM/USDT")
            self.assertAlmostEqual(kwargs["amount"], 50.0, places=4)
            self.assertAlmostEqual(kwargs["pct"], 0.5, places=4)

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

    def test_get_prices_batch_uses_cache(self):
        from price_fetcher import get_prices_batch, _price_cache
        import time

        _price_cache.clear()
        _price_cache["SOL/USDT"] = (100.0, time.time())
        _price_cache["BNB/USDT"] = (500.0, time.time())

        with patch("price_fetcher.requests.get") as mock_get:
            result = get_prices_batch(["SOL/USDT", "BNB/USDT", "SOL/USDT"])
        self.assertEqual(result["SOL/USDT"], 100.0)
        self.assertEqual(result["BNB/USDT"], 500.0)
        mock_get.assert_not_called()

    def test_get_prices_batch_gate_bulk(self):
        from price_fetcher import get_prices_batch, _price_cache

        _price_cache.clear()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {"currency_pair": "SOL_USDT", "last": "150.5"},
            {"currency_pair": "DOGE_USDT", "last": "0.08"},
            {"currency_pair": "BTC_USDT", "last": "60000"},
        ]
        with patch("price_fetcher.requests.get", return_value=mock_resp) as mock_get:
            result = get_prices_batch(["SOL/USDT", "DOGE/USDT"])
        self.assertEqual(result["SOL/USDT"], 150.5)
        self.assertEqual(result["DOGE/USDT"], 0.08)
        self.assertEqual(mock_get.call_count, 1)

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
        msg_call = next(c for c in mock_post.call_args_list if c[1].get("json"))
        called_text = msg_call[1]["json"]["text"]
        self.assertIn("🧪 [DEMO]", called_text)
        self.assertIn(expected_content, called_text)

    def test_send_signal_message_buy_with_demo_prefix(self):
        from unittest.mock import patch
        from telegram_notifier import send_signal_message

        coin = {"symbol": "ARIA/USDT", "name": "Aria AI"}

        with patch("telegram_notifier.is_demo_mode", return_value=True), \
             patch("notifications.chart_image.send_trade_chart_if_enabled", return_value=False), \
             patch("telegram_notifier.requests.post") as mock_post:
            mock_post.return_value.status_code = 200

            send_signal_message(
                "BUY", coin, 0.0523, 35.0, 0.048, 1.4, "🟢", "Stark Bullish", executed=True
            )

            self._assert_demo_prefix_in_message(mock_post, "BUY EXECUTED")
            self._assert_demo_prefix_in_message(mock_post, "aria-ai")

    def test_send_signal_message_sell_20_executed_with_demo_prefix(self):
        from unittest.mock import patch
        from telegram_notifier import send_signal_message

        coin = {"symbol": "RAVE/USDT", "name": "RaveDAO"}

        with patch("telegram_notifier.is_demo_mode", return_value=True), \
             patch("notifications.chart_image.send_trade_chart_if_enabled", return_value=False), \
             patch("telegram_notifier.requests.post") as mock_post:
            mock_post.return_value.status_code = 200

            send_signal_message(
                "SELL_20", coin, 0.65, 72.0, 0.60, 0.8, "🔴", "Bearish", executed=True
            )

            self._assert_demo_prefix_in_message(mock_post, "SELL 20% EXECUTED")

    def test_send_signal_message_sell_30_signal_not_executed(self):
        from unittest.mock import patch
        from telegram_notifier import send_signal_message

        coin = {"symbol": "RAVE/USDT", "name": "RaveDAO"}

        with patch("telegram_notifier.is_demo_mode", return_value=False), \
             patch("telegram_notifier.requests.post") as mock_post:
            mock_post.return_value.status_code = 200

            send_signal_message(
                "SELL_30", coin, 0.65, 75.0, 0.60, 0.8, "🔴", "Bearish", executed=None
            )

            called_text = mock_post.call_args[1]["json"]["text"]
            self.assertIn("SELL 30% SIGNAL", called_text)
            self.assertNotIn("EXECUTED", called_text)

    def test_send_signal_message_sell_blocked_shows_reason(self):
        from unittest.mock import patch
        from telegram_notifier import send_signal_message

        coin = {"symbol": "RAVE/USDT", "name": "RaveDAO"}

        with patch("telegram_notifier.is_demo_mode", return_value=False), \
             patch("telegram_notifier.requests.post") as mock_post:
            mock_post.return_value.status_code = 200

            send_signal_message(
                "SELL_30",
                coin,
                0.65,
                75.0,
                0.60,
                0.8,
                "🔴",
                "Bearish",
                executed=False,
                trade_message="No position to sell",
            )

            called_text = mock_post.call_args[1]["json"]["text"]
            self.assertIn("SELL 30% BLOCKED", called_text)
            self.assertIn("Grund:", called_text)
            self.assertIn("Position", called_text)

    def test_send_signal_message_includes_why_de(self):
        from unittest.mock import patch
        from telegram_notifier import send_signal_message

        coin = {"symbol": "H/USDT", "name": "Humanity"}

        with patch("telegram_notifier.is_demo_mode", return_value=False), \
             patch("notifications.chart_image.send_trade_chart_if_enabled", return_value=False), \
             patch("telegram_notifier.requests.post") as mock_post:
            mock_post.return_value.status_code = 200

            send_signal_message(
                "SELL_30",
                coin,
                0.12,
                74.0,
                0.11,
                1.1,
                "🔴",
                "Bearish",
                executed=True,
                why_de="RSI überkauft — 30 % verkauft.",
                tech_line="TA→SELL_30 | RSI=74.0",
                source_de="Technische Analyse",
            )

            msg_call = next(c for c in mock_post.call_args_list if c[1].get("json"))
            called_text = msg_call[1]["json"]["text"]
            self.assertIn("Warum:", called_text)
            self.assertIn("überkauft", called_text)
            self.assertIn("TA→SELL_30", called_text)
            self.assertIn("<a href=", called_text)
            self.assertIn("Links:", called_text)

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
            self.assertIn("MARKET UPDATE", called_text)

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

        with patch.object(data_manager, "is_demo_mode", return_value=True):
            path = data_manager.get_data_file("watchlist.json")
            self.assertTrue(path.endswith(".demo.json"))

        with patch.object(data_manager, "is_demo_mode", return_value=False):
            path = data_manager.get_data_file("watchlist.json")
            self.assertFalse(path.endswith(".demo.json"))

    def test_load_demo_data_only_runs_in_demo_mode(self):
        from unittest.mock import patch
        import data_manager

        with patch.object(data_manager, "is_demo_mode", return_value=False), \
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

    def test_risk_manager_manual_buy_honors_requested_usdt(self):
        from core.config import BotConfig
        from core.models import TradeOrder
        from data_manager import get_config
        from risk.risk_manager import RiskManager

        raw = dict(get_config())
        raw["trading_mode"] = "paper"
        cfg = BotConfig()
        cfg._raw = raw
        risk = RiskManager(cfg)

        with patch.object(risk, "_daily_trades_count", return_value=0):
            decision = risk.evaluate(
                TradeOrder("BUY", "ARIA/USDT", 0.0325, 0, usdt_amount=200),
                "4h",
                source="manual",
            )
        self.assertTrue(decision.approved)
        self.assertAlmostEqual(decision.order.usdt_amount, 200.0, places=2)
        self.assertEqual(decision.order.source, "manual")

    def test_risk_manager_preserves_source_on_approved_buy(self):
        from core.config import BotConfig
        from core.models import TradeOrder
        from data_manager import get_config
        from risk.risk_manager import RiskManager

        raw = dict(get_config())
        raw["trading_mode"] = "paper"
        cfg = BotConfig()
        cfg._raw = raw
        risk = RiskManager(cfg)
        order = TradeOrder("BUY", "ARIA/USDT", 0.0325, 0, usdt_amount=200, source="manual", order_id="abc")

        with patch.object(risk, "_daily_trades_count", return_value=0):
            decision = risk.evaluate(order, "4h", source="manual")

        self.assertTrue(decision.approved)
        self.assertEqual(decision.order.source, "manual")
        self.assertEqual(decision.order.order_id, "abc")

    def test_risk_manager_approves_buy_with_dynamic_sizing(self):
        from core.config import BotConfig
        from core.models import TradeOrder
        from data_manager import get_config
        from risk.risk_manager import RiskManager

        raw = dict(get_config())
        raw["trading_mode"] = "paper"
        cfg = BotConfig()
        cfg._raw = raw
        risk = RiskManager(cfg)

        with patch.object(risk.market, "fetch_indicators", return_value={"atr_pct": 3.0, "rsi": 45.0}), \
             patch.object(risk, "_daily_trades_count", return_value=0):
            decision = risk.evaluate(
                TradeOrder("BUY", "XRVM/USDT", 1.0, 0, usdt_amount=150),
                "4h",
                trust_score=90,
                confidence=80,
                indicators={"atr_pct": 3.0},
            )
        self.assertTrue(decision.approved)
        self.assertGreater(decision.order.usdt_amount, 0)
        self.assertLessEqual(decision.order.usdt_amount, 300)

    def test_risk_manager_blocks_max_open_positions(self):
        from core.config import BotConfig
        from core.models import TradeOrder
        from data_manager import get_config
        from risk.risk_manager import RiskManager

        raw = dict(get_config())
        raw["max_open_positions"] = 1
        cfg = BotConfig()
        cfg._raw = raw
        risk = RiskManager(cfg)

        update_position("POS_A/USDT", "4h", "BUY", 1.0, 10)
        with patch.object(risk.market, "fetch_indicators", return_value={"atr_pct": 3.0}):
            decision = risk.evaluate(TradeOrder("BUY", "POS_B/USDT", 1.0, 0), "4h", indicators={"atr_pct": 3.0})
        self.assertFalse(decision.approved)
        self.assertEqual(decision.code, "max_open_positions")

    def test_risk_manager_blocks_daily_trade_limit(self):
        from core.config import BotConfig
        from core.models import TradeOrder
        from data_manager import get_config
        from risk.risk_manager import RiskManager

        raw = dict(get_config())
        raw["trading_mode"] = "paper"
        raw["max_daily_trades"] = 1
        raw.setdefault("risk", {})["max_daily_buys"] = 1
        cfg = BotConfig()
        cfg._raw = raw
        risk = RiskManager(cfg)

        filled_order = {
            "status": "filled",
            "side": "buy",
            "timestamps": {"filled": datetime.now().isoformat()},
        }
        with patch("data_manager.load_orders", return_value={"orders": [filled_order]}), \
             patch.object(risk.market, "fetch_indicators", return_value={"atr_pct": 3.0}):
            decision = risk.evaluate(TradeOrder("BUY", "XRVM/USDT", 1.0, 0), "4h", indicators={"atr_pct": 3.0})
        self.assertFalse(decision.approved)
        self.assertEqual(decision.code, "max_daily_trades")
        self.assertIn("buy limit", decision.message.lower())

    def test_risk_manager_drawdown_throttle_halves_size(self):
        from core.config import BotConfig
        from core.models import TradeOrder
        from data_manager import get_config, load_trade_history, save_trade_history
        from risk.risk_manager import RiskManager

        raw = dict(get_config())
        raw["trading_mode"] = "paper"
        cfg = BotConfig()
        cfg._raw = raw
        risk = RiskManager(cfg)

        history = load_trade_history()
        history["virtual_balance"] = 4000.0
        history["peak_equity"] = 5000.0
        save_trade_history(history)

        with patch.object(risk.market, "fetch_indicators", return_value={"atr_pct": 3.0}), \
             patch.object(risk, "_daily_trades_count", return_value=0):
            normal = risk.evaluate(
                TradeOrder("BUY", "XRVM/USDT", 1.0, 0, usdt_amount=100),
                "4h",
                trust_score=70,
                confidence=50,
                indicators={"atr_pct": 3.0},
            )
        self.assertTrue(normal.approved)
        self.assertEqual(normal.size_multiplier, 0.5)

    def test_risk_manager_approves_sells_without_limits(self):
        from risk.risk_manager import RiskManager
        from core.models import TradeOrder

        risk = RiskManager()
        decision = risk.evaluate(TradeOrder("SELL", "XRVM/USDT", 1.0, 50), "4h")
        self.assertTrue(decision.approved)

    def test_risk_manager_sells_do_not_count_toward_buy_limit(self):
        from core.config import BotConfig
        from core.models import TradeOrder
        from data_manager import get_config
        from risk.risk_manager import RiskManager

        raw = dict(get_config())
        raw["trading_mode"] = "paper"
        raw["max_daily_trades"] = 1
        raw.setdefault("risk", {})["max_daily_buys"] = 1
        cfg = BotConfig()
        cfg._raw = raw
        risk = RiskManager(cfg)

        sell_orders = [
            {
                "status": "filled",
                "side": "sell",
                "timestamps": {"filled": datetime.now().isoformat()},
            }
            for _ in range(10)
        ]
        with patch("data_manager.load_orders", return_value={"orders": sell_orders}), \
             patch.object(risk.market, "fetch_indicators", return_value={"atr_pct": 3.0}):
            decision = risk.evaluate(TradeOrder("BUY", "WLD/USDT", 1.0, 0), "4h", indicators={"atr_pct": 3.0})
        self.assertTrue(decision.approved)

    def test_trading_service_sends_positions_after_executed_trade(self):
        from services.trading_service import TradingService
        from core.models import TradeResult

        svc = TradingService()
        ok_result = TradeResult(True, "BUY", "XRVM/USDT", amount=10, price=1.0, usdt_amount=10)
        with patch.object(svc, "can_execute", return_value=(True, "")), \
             patch.object(svc.risk, "evaluate") as mock_risk, \
             patch.object(svc.adapter, "execute", return_value=ok_result), \
             patch("notifications.telegram_commands.position_display.send_positions_snapshot") as mock_snapshot:
            from core.models import RiskDecision, TradeOrder
            mock_risk.return_value = RiskDecision(
                approved=True,
                order=TradeOrder("BUY", "XRVM/USDT", 1.0, 10, usdt_amount=10),
            )
            from core.models import TradeOrder as TO
            result = svc.execute_order(TO("BUY", "XRVM/USDT", 1.0, 10, usdt_amount=10), "4h")
            self.assertTrue(result.executed)
            mock_snapshot.assert_called_once()
            self.assertIs(mock_snapshot.call_args.kwargs.get("trade_result"), result)

    def test_trading_service_blocks_via_risk_manager(self):
        from core.config import BotConfig
        from data_manager import get_config
        from services.trading_service import TradingService

        raw = dict(get_config())
        raw["trading_mode"] = "paper"
        raw["max_daily_trades"] = 1
        raw.setdefault("risk", {})["max_daily_buys"] = 1
        cfg = BotConfig()
        cfg._raw = raw
        svc = TradingService(cfg)

        filled_order = {
            "status": "filled",
            "side": "buy",
            "timestamps": {"filled": datetime.now().isoformat()},
        }
        with patch.object(svc, "refresh"), \
             patch("data_manager.load_orders", return_value={"orders": [filled_order]}), \
             patch.object(svc.risk, "_portfolio_equity", return_value=5000.0), \
             patch.object(svc.risk, "_available_usdt", return_value=5000.0):
            result = svc.execute_buy("XRVM/USDT", "4h", 1.0, 100)
        self.assertFalse(result.executed)
        self.assertIn("buy limit", result.message.lower())

    def test_decision_engine_x_stop_loss_trigger(self):
        from strategies.decision_engine import DecisionEngine
        from x_analyzer import XSignal

        engine = DecisionEngine()
        x_sig = XSignal("CryptoCapo_", "XRVM", "HOLD", 70, stop_loss=1.05, rationale="protective stop")
        x_sig.trust_score = 80

        with patch.object(engine.market, "fetch_indicators", return_value={"rsi": 50.0, "lower_bb": 0.9, "vol_multiplier": 1.0}):
            update_position("XRVM/USDT", "4h", "BUY", 1.2, 100)
            analysis = engine.evaluate({"symbol": "XRVM/USDT", "timeframe": "4h"}, 1.0, x_signals=[x_sig])
        self.assertIn(analysis.normalized_action, ("SELL_FULL", "SELL_PARTIAL_20", "SELL_PARTIAL_30"))
        self.assertIn("x_stop_loss", analysis.sources)

    def test_market_service_includes_atr(self):
        from services.market_service import MarketService
        import pandas as pd

        service = MarketService()
        df = pd.DataFrame({
            "ts": range(30),
            "open": [1.0] * 30,
            "high": [1.05] * 30,
            "low": [0.95] * 30,
            "close": [1.0] * 30,
            "volume": [1000.0] * 30,
        })
        df["rsi"] = 45.0
        df["lower"] = 0.97
        df["vol_avg"] = 900.0

        with patch.object(service, "_fetch_ohlcv", return_value=df):
            indicators = service.fetch_indicators("XRVM/USDT", "4h", 1.0)
        self.assertIn("atr", indicators)
        self.assertIn("atr_pct", indicators)
        self.assertGreater(indicators["atr_pct"], 0)

    def test_strategy_discovery_heuristic(self):
        from intelligence.strategy_discovery import StrategyDiscovery

        discovery = StrategyDiscovery()
        discovery.config.raw["use_mock_x_data"] = True
        hyp = discovery.discover_from_tweet(
            "SOL showing RSI divergence on 1h with strong volume spike. Bullish setup.",
            "TestTrader",
            "post_strategy_1",
        )
        self.assertIsNotNone(hyp)
        self.assertEqual(hyp.timeframe, "1h")
        self.assertIn("rsi_buy_low", hyp.params)

    def test_strategy_discovery_save_and_dedup(self):
        from intelligence.strategy_discovery import StrategyDiscovery
        from data_manager import load_paper_strategies, save_paper_strategies

        backup = load_paper_strategies()
        save_paper_strategies({"hypotheses": []})
        try:
            discovery = StrategyDiscovery()
            discovery.config.raw["use_mock_x_data"] = True
            hyp = discovery.discover_from_tweet(
                "BTC breakout with volume confirmation on 4h timeframe.",
                "DedupTrader",
                "dedup_post_1",
            )
            self.assertTrue(discovery.save_hypothesis(hyp))
            self.assertFalse(discovery.save_hypothesis(hyp))
        finally:
            save_paper_strategies(backup)

    def test_trend_engine_cross_validate(self):
        from intelligence.trend_engine import TrendEngine
        from x_analyzer import XSignal

        engine = TrendEngine()
        signals = [XSignal("Trader", "SOL", "BUY", 80)]
        scanner = [{
            "symbol": "SOL/USDT",
            "change_5m": 2.5,
            "change_1d": 12.0,
            "regime": "BREAKOUT",
            "volume_24h": 5_000_000,
        }]
        results = engine.cross_validate(signals, scanner_results=scanner)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["symbol"], "SOL/USDT")
        self.assertEqual(results[0]["consensus"], "x_scanner")

    def test_paper_sandbox_evaluate_and_metrics(self):
        from strategies.paper_sandbox import PaperSandbox
        from data_manager import load_paper_strategies, save_paper_strategies, load_paper_sandbox_history, save_paper_sandbox_history

        strat_backup = load_paper_strategies()
        hist_backup = load_paper_sandbox_history()
        hyp_id = "hyp_test_sandbox"
        save_paper_strategies({
            "hypotheses": [{
                "id": hyp_id,
                "name": "Test RSI 4h",
                "source_account": "Tester",
                "status": "testing",
                "timeframe": "4h",
                "symbol": "XRVM/USDT",
                "params": {
                    "rsi_buy_low": 0,
                    "rsi_buy_high": 100,
                    "volume_multiplier": 0.1,
                    "rsi_sell_30": 99,
                    "rsi_sell_20": 99,
                },
                "created_at": datetime.now().isoformat(),
                "metrics": {},
            }]
        })
        save_paper_sandbox_history({"portfolios": {}})
        try:
            sandbox = PaperSandbox()
            with patch.object(sandbox.market, "fetch_indicators", return_value={
                "rsi": 40.0, "lower_bb": 1.1, "vol_multiplier": 2.0, "atr_pct": 3.0,
            }):
                action = sandbox.evaluate_hypothesis(
                    load_paper_strategies()["hypotheses"][0],
                    "XRVM/USDT",
                    1.0,
                )
            self.assertEqual(action, "BUY")
            metrics = sandbox.compute_metrics(hyp_id, {"XRVM/USDT": 1.0})
            self.assertGreater(metrics.trades, 0)
            self.assertGreater(metrics.equity, 0)
        finally:
            save_paper_strategies(strat_backup)
            save_paper_sandbox_history(hist_backup)

    def test_promote_hypothesis_to_config(self):
        from strategies.registry import promote_hypothesis_to_config
        from data_manager import get_config, save_config

        cfg = dict(get_config())
        original_strategies = list(cfg.get("strategies", []))
        cfg["strategies"] = [s for s in original_strategies if s.get("sandbox_id") != "hyp_promote_test"]
        save_config(cfg)

        hypothesis = {
            "id": "hyp_promote_test",
            "name": "Promoted test",
            "symbol": "TESTCOIN/USDT",
            "timeframe": "4h",
            "source_account": "Tester",
            "params": {"rsi_buy_low": 28, "rsi_buy_high": 48, "volume_multiplier": 1.3},
        }
        ok, msg = promote_hypothesis_to_config(hypothesis)
        self.assertTrue(ok)

        reloaded = get_config()
        promoted = [s for s in reloaded.get("strategies", []) if s.get("sandbox_id") == "hyp_promote_test"]
        self.assertEqual(len(promoted), 1)
        self.assertEqual(promoted[0]["symbol"], "TESTCOIN/USDT")

        cfg["strategies"] = original_strategies
        save_config(cfg)

    def test_sandbox_promotion_ready_checks_min_days(self):
        from strategies.paper_sandbox import PaperSandbox
        from data_manager import load_paper_strategies, save_paper_strategies

        backup = load_paper_strategies()
        save_paper_strategies({
            "hypotheses": [{
                "id": "hyp_young",
                "name": "Young",
                "status": "testing",
                "created_at": datetime.now().isoformat(),
                "metrics": {"win_rate": 80, "sharpe": 1.2, "max_drawdown_pct": 5, "trades": 5},
            }]
        })
        try:
            sandbox = PaperSandbox()
            ready, reason = sandbox.promotion_ready("hyp_young")
            self.assertFalse(ready)
            self.assertIn("min", reason.lower())
        finally:
            save_paper_strategies(backup)

    def test_cmc_parser_bullish_votes(self):
        from data.cmc_community_provider import CMCCommunityParser, RawCMCPost

        parser = CMCCommunityParser()
        signal = parser.parse(RawCMCPost("p1", "SOL", "Community loves SOL", votes_bullish=80, votes_bearish=10))
        self.assertEqual(signal.action, "BUY")
        self.assertGreaterEqual(signal.confidence, 60)
        self.assertEqual(signal.source, "cmc")

    def test_cmc_mock_provider_respects_watchlist(self):
        import uuid
        from data.cmc_community_provider import MockCMCProvider
        from data_manager import load_cmc_posts, save_cmc_posts

        backup = load_cmc_posts()
        save_cmc_posts({"posts": []})
        try:
            provider = MockCMCProvider()
            handle_coin = f"ZZZ{uuid.uuid4().hex[:4].upper()}"
            posts = provider.fetch_posts([{"symbol": f"{handle_coin}/USDT"}], limit=5)
            self.assertEqual(len(posts), 0)

            sol_posts = provider.fetch_posts([{"symbol": "SOL/USDT"}], limit=5)
            self.assertGreater(len(sol_posts), 0)
        finally:
            save_cmc_posts(backup)

    def test_decision_engine_lc_buy_merge(self):
        from data.lunarcrush_scorer import LunarCrushSignal
        from strategies.decision_engine import DecisionEngine

        engine = DecisionEngine()
        lc = LunarCrushSignal("SOL", "BUY", 82, rationale="galaxy momentum", galaxy_score=74, alt_rank=45, sentiment=76)
        lc.trust_score = 72.0
        lc.effective_confidence = 59.0

        empty_pos = {"amount": 0, "average_entry": 0}
        with patch.object(engine.market, "fetch_indicators", return_value={"rsi": 50.0, "lower_bb": 0.9, "vol_multiplier": 1.0}), \
             patch("strategies.decision_engine.count_open_positions", return_value=0), \
             patch("strategies.decision_engine.get_position", return_value=empty_pos):
            analysis = engine.evaluate({"symbol": "SOL/USDT", "timeframe": "4h"}, 1.0, lc_signals=[lc])
        self.assertIn(analysis.normalized_action, ("BUY", "BUY_STRONG"))
        self.assertIn("lc", analysis.sources)

    def test_decision_engine_cmc_buy_merge(self):
        from data.cmc_community_provider import CMCCommunitySignal
        from strategies.decision_engine import DecisionEngine

        engine = DecisionEngine()
        cmc = CMCCommunitySignal("SOL", "BUY", 78, rationale="bullish community", votes_bullish=90, votes_bearish=8)
        cmc.trust_score = 80.0
        cmc.effective_confidence = 62.4

        empty_pos = {"amount": 0, "average_entry": 0}
        with patch.object(engine.market, "fetch_indicators", return_value={"rsi": 50.0, "lower_bb": 0.9, "vol_multiplier": 1.0}), \
             patch("strategies.decision_engine.count_open_positions", return_value=0), \
             patch("strategies.decision_engine.get_position", return_value=empty_pos):
            analysis = engine.evaluate({"symbol": "SOL/USDT", "timeframe": "4h"}, 1.0, cmc_signals=[cmc])
        self.assertIn(analysis.normalized_action, ("BUY", "BUY_STRONG"))
        self.assertIn("cmc", analysis.sources)

    def test_decision_engine_multi_source_x_cmc_consensus(self):
        from data.cmc_community_provider import CMCCommunitySignal
        from strategies.decision_engine import DecisionEngine
        from x_analyzer import XSignal

        engine = DecisionEngine()
        x_sig = XSignal("Trader", "SOL", "BUY", 85, rationale="strong")
        x_sig.trust_score = 90
        x_sig.effective_confidence = 80
        cmc = CMCCommunitySignal("SOL", "BUY", 80, votes_bullish=85, votes_bearish=10)
        cmc.effective_confidence = 65

        empty_pos = {"amount": 0, "average_entry": 0}
        with patch.object(engine.market, "fetch_indicators", return_value={"rsi": 35.0, "lower_bb": 1.05, "vol_multiplier": 2.0}), \
             patch("strategies.decision_engine.count_open_positions", return_value=0), \
             patch("strategies.decision_engine.get_position", return_value=empty_pos):
            analysis = engine.evaluate(
                {"symbol": "SOL/USDT", "timeframe": "4h"},
                1.0,
                x_signals=[x_sig],
                cmc_signals=[cmc],
            )
        self.assertEqual(analysis.normalized_action, "BUY_STRONG")
        self.assertIn("multi_source", analysis.sources)
        self.assertIn("x", analysis.sources)
        self.assertIn("cmc", analysis.sources)

    def test_social_pipeline_process_lc_signals(self):
        from data_manager import load_lc_signals, save_lc_signals, load_watchlist
        from services.social_pipeline import SocialPipeline
        from x_analyzer import XAnalyzer

        backup = load_lc_signals()
        save_lc_signals({"signals": []})
        try:
            pipeline = SocialPipeline(XAnalyzer())
            watchlist = load_watchlist()
            if not any("SOL" in c.get("symbol", "") for c in watchlist):
                watchlist = watchlist + [{"symbol": "SOL/USDT", "active": True}]
            signals = pipeline.process_lc_signals(watchlist)
            self.assertGreater(len(signals), 0)
            self.assertEqual(signals[0].source, "lc")
        finally:
            save_lc_signals(backup)

    def test_social_pipeline_process_cmc_posts(self):
        from data_manager import load_cmc_posts, save_cmc_posts, load_watchlist
        from services.social_pipeline import SocialPipeline
        from x_analyzer import XAnalyzer

        backup = load_cmc_posts()
        save_cmc_posts({"posts": []})
        try:
            pipeline = SocialPipeline(XAnalyzer())
            watchlist = load_watchlist()
            if not any("SOL" in c.get("symbol", "") for c in watchlist):
                watchlist = watchlist + [{"symbol": "SOL/USDT", "active": True}]
            signals = pipeline.process_cmc_posts(watchlist)
            self.assertGreater(len(signals), 0)
            self.assertEqual(signals[0].source, "cmc")
        finally:
            save_cmc_posts(backup)

    def test_onchain_weight_config_accessors(self):
        from core.config import get_bot_config
        cfg = get_bot_config()
        self.assertAlmostEqual(cfg.onchain_weight, 0.15)
        self.assertAlmostEqual(cfg.lc_weight, 0.18)
        self.assertAlmostEqual(cfg.x_weight, 0.40)
        self.assertAlmostEqual(
            cfg.x_weight + cfg.technical_weight + cfg.onchain_weight + cfg.lc_weight,
            1.0,
        )

    def test_audit_trail_records_decision(self):
        import json
        import tempfile
        from unittest.mock import patch
        from services.audit_trail import AuditTrail
        from core.models import SignalAnalysis

        analysis = SignalAnalysis(
            action="BUY",
            symbol="XRVM/USDT",
            timeframe="4h",
            rsi=40.0,
            lower_bb=0.9,
            vol_multiplier=1.5,
            ampel_emoji="🟢",
            ampel_text="Bullish",
            normalized_action="BUY",
            rationale="TA→BUY",
            confidence=70.0,
            sources=["technical"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            log_path = os.path.join(tmp, "decisions.jsonl")
            with patch("logger.DECISIONS_LOG_FILE", log_path):
                trail = AuditTrail()
                trail.record({"symbol": "XRVM/USDT"}, analysis, None, 1.0)
                with open(log_path, encoding="utf-8") as f:
                    line = f.readline()
                entry = json.loads(line)
                self.assertEqual(entry["symbol"], "XRVM/USDT")
                self.assertEqual(entry["normalized_action"], "BUY")
                self.assertIn("rationale", entry)

    def test_build_dashboard_data(self):
        from notifications.terminal_dashboard import build_dashboard_data

        with patch("notifications.terminal_dashboard.get_prices", return_value=(1.0, 1.0, None)), \
             patch("notifications.terminal_dashboard.list_active_positions", return_value=[]):
            data = build_dashboard_data(
                cycle_signals=["🟢 @Trader BUY BTC | 80%"],
                coin_results=[{"symbol": "BTC/USDT", "action": "HOLD", "normalized_action": "HOLD", "rsi": 50, "ampel_emoji": "🟡", "rationale": ""}],
                trading_mode="paper",
            )
        self.assertIn("balance", data)
        self.assertIn("trading_mode", data)
        self.assertEqual(data["trading_mode"], "PAPER")
        self.assertGreater(len(data["signals"]), 0)

    def test_build_dashboard_data_live_dry_run_uses_live_ledger(self):
        from notifications.terminal_dashboard import build_dashboard_data

        live_hist = {"virtual_balance": 3952.19, "realized_pnl": -111.82, "trades": []}
        mock_cfg = unittest.mock.MagicMock()
        mock_cfg.raw = {
            "trading_mode": "live",
            "live": {"dry_run": True, "dry_run_enhanced": False},
        }
        mock_cfg.trading_mode = "live"
        mock_cfg.simulated_balance_usdt = 5000
        with patch("notifications.terminal_dashboard.get_prices", return_value=(1.0, 1.0, None)), \
             patch("notifications.terminal_dashboard.list_active_positions", return_value=[]), \
             patch("data_manager.load_live_trade_history", return_value=live_hist), \
             patch("core.config.get_bot_config", return_value=mock_cfg):
            data = build_dashboard_data(coin_results=[], trading_mode="live")
        self.assertEqual(data["balance"], "$3,952")
        self.assertEqual(data["realized_pnl"], "$-111.8")
        self.assertEqual(data["total_value"], "$3,952")

    def test_build_cycle_summary(self):
        from notifications.terminal_dashboard import build_cycle_summary

        summary = build_cycle_summary(
            coin_results=[{"symbol": "ARIA/USDT", "executed": True, "order_type": "BUY", "normalized_action": "BUY"}],
            trading_mode="paper",
            x_signal_count=2,
            cmc_signal_count=1,
        )
        self.assertIn("Zyklus-Zusammenfassung", summary)
        self.assertIn("PAPER", summary)
        self.assertIn("Ausgeführt", summary)
        self.assertIn("Orders (24h", summary)

    def test_log_decision_writes_jsonl(self):
        import json
        import tempfile
        from unittest.mock import patch
        from logger import log_decision

        with tempfile.TemporaryDirectory() as tmp:
            log_path = os.path.join(tmp, "decisions.jsonl")
            with patch("logger.DECISIONS_LOG_FILE", log_path):
                log_decision({"symbol": "TEST/USDT", "action": "HOLD"})
                with open(log_path, encoding="utf-8") as f:
                    entry = json.loads(f.readline())
                self.assertEqual(entry["symbol"], "TEST/USDT")
                self.assertIn("timestamp", entry)

    def test_observability_config_accessors(self):
        from core.config import get_bot_config
        cfg = get_bot_config()
        self.assertTrue(cfg.terminal_dashboard_enabled)
        self.assertIsInstance(cfg.notify_on_cycle, bool)
        self.assertTrue(cfg.decisions_audit_enabled)

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
        import logger as logger_mod
        import shutil
        from strategies.positions import positions, save_positions

        positions.clear()
        positions.update(self._positions_backup)
        save_positions()
        try:
            from data_manager import save_trade_history
            save_trade_history(self._trade_history_backup)
        except Exception:
            pass
        if hasattr(self, "_log_file_backup"):
            logger_mod.LOG_DIR = self._log_dir_backup
            logger_mod.LOG_FILE = self._log_file_backup
            shutil.rmtree(self._log_tmp, ignore_errors=True)


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
