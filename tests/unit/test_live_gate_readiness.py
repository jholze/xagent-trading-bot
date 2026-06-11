import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.config import BotConfig
from core.models import TradeOrder
from data_manager import get_config
from execution.gate_adapter import GateExecutionAdapter
from notifications.telegram_commands import mode_commands
from risk.risk_manager import RiskManager
from data_manager import uses_exchange_ledger


class TestLiveGateReadiness(unittest.TestCase):
    def _live_config(self):
        raw = dict(get_config())
        raw["trading_mode"] = "live"
        raw["live_confirmed"] = True
        cfg = BotConfig()
        cfg._raw = raw
        return cfg

    def test_uses_exchange_ledger(self):
        self.assertTrue(uses_exchange_ledger("live"))
        self.assertTrue(uses_exchange_ledger("gate_testnet"))
        self.assertFalse(uses_exchange_ledger("paper"))

    def test_risk_manager_caps_buy_to_gate_usdt(self):
        cfg = self._live_config()
        rm = RiskManager(cfg)
        with patch("risk.risk_manager.fetch_usdt_balance", return_value=30.0), \
             patch("risk.risk_manager.fetch_portfolio_equity", return_value=500.0), \
             patch("risk.risk_manager.count_open_positions", return_value=1), \
             patch("risk.risk_manager.get_position", return_value={"amount": 0}), \
             patch.object(rm, "_daily_trades_count", return_value=0):
            decision = rm.evaluate(
                TradeOrder("BUY", "SOL/USDT", 100, 0, usdt_amount=200),
                source="manual",
            )
        self.assertTrue(decision.approved)
        self.assertLessEqual(decision.order.usdt_amount, 30.0)

    def test_risk_status_summary_uses_gate_ledger(self):
        cfg = self._live_config()
        rm = RiskManager(cfg)
        with patch("risk.risk_manager.fetch_usdt_balance", return_value=123.45), \
             patch("risk.risk_manager.fetch_portfolio_equity", return_value=400.0), \
             patch("risk.risk_manager.count_open_positions", return_value=2), \
             patch.object(rm, "_daily_trades_count", return_value=1):
            summary = rm.status_summary()
        self.assertEqual(summary["ledger_source"], "gate")
        self.assertEqual(summary["virtual_balance"], 123.45)

    def test_gate_adapter_skips_virtual_ledger_in_live(self):
        cfg = self._live_config()
        adapter = GateExecutionAdapter(cfg, testnet=False)
        order = TradeOrder("BUY", "SOL/USDT", 100, 0.5, usdt_amount=25)
        with patch.object(adapter.portfolio, "execute_buy") as mock_buy:
            from core.models import TradeResult
            mock_buy.return_value = TradeResult(True, "BUY", "SOL/USDT", amount=0.5, price=100, usdt_amount=25)
            with patch("execution.gate_adapter.record_live_trade"):
                adapter._sync_local_ledger(order, "4h", exchange_order_id="ex1")
        mock_buy.assert_called_once()
        self.assertFalse(mock_buy.call_args.kwargs.get("sync_virtual_ledger", True))

    def test_live_confirm_blocked_in_demo(self):
        with patch("notifications.telegram_commands.mode_commands.is_demo_mode", return_value=True), \
             patch("notifications.telegram_commands.mode_commands.send_telegram_message") as mock_send:
            self.assertTrue(mode_commands.handle("/live_confirm"))
            self.assertIn("Demo", mock_send.call_args[0][0])

    def test_live_confirm_blocked_without_keys(self):
        with patch("notifications.telegram_commands.mode_commands.is_demo_mode", return_value=False), \
             patch.dict(os.environ, {}, clear=True), \
             patch("notifications.telegram_commands.mode_commands.send_telegram_message") as mock_send:
            self.assertTrue(mode_commands.handle("/live_confirm"))
            self.assertIn("Keys fehlen", mock_send.call_args[0][0])

    def test_live_confirm_warns_on_dry_run(self):
        with patch("notifications.telegram_commands.mode_commands.is_demo_mode", return_value=False), \
             patch.dict(os.environ, {"GATE_API_KEY": "k", "GATE_API_SECRET": "s"}), \
             patch("notifications.telegram_commands.mode_commands._save_mode_updates", return_value=True), \
             patch("notifications.telegram_commands.mode_commands.reload_config"), \
             patch("notifications.telegram_commands.mode_commands.send_telegram_message") as mock_send:
            self.assertTrue(mode_commands.handle("/live_confirm"))
            self.assertIn("dry_run", mock_send.call_args[0][0])


if __name__ == "__main__":
    unittest.main()