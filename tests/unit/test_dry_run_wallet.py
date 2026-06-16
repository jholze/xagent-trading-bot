import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.config import BotConfig
from core.models import TradeOrder
from data_manager import (
    LIVE_TRADE_HISTORY_FILE,
    compute_sim_cash_from_trades,
    get_config,
    is_dry_run_enhanced,
    load_live_trade_history,
    record_live_trade,
    reload_config,
)
from risk.risk_manager import RiskManager
from services.gate_balance import fetch_usdt_balance


class TestDryRunWallet(unittest.TestCase):
    def _enhanced_config(self):
        raw = dict(get_config())
        raw["trading_mode"] = "live"
        raw.setdefault("live", {})["dry_run"] = True
        raw["live"]["dry_run_enhanced"] = True
        raw["live"]["simulated_balance_usdt"] = 5000
        cfg = BotConfig()
        cfg._raw = raw
        return cfg

    def test_is_dry_run_enhanced_requires_all_flags(self):
        self.assertFalse(is_dry_run_enhanced({"trading_mode": "paper", "live": {"dry_run": True, "dry_run_enhanced": True}}))
        self.assertFalse(is_dry_run_enhanced({"trading_mode": "live", "live": {"dry_run": False, "dry_run_enhanced": True}}))
        self.assertTrue(is_dry_run_enhanced({"trading_mode": "live", "live": {"dry_run": True, "dry_run_enhanced": True}}))

    def test_record_live_trade_updates_virtual_balance(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "live_trade_history.json")
            cfg = {
                "trading_mode": "live",
                "live": {"dry_run": True, "dry_run_enhanced": True, "simulated_balance_usdt": 5000},
            }
            with patch("data_manager.get_data_file", return_value=path), \
                 patch("data_manager.get_config", return_value=cfg), \
                 patch("data_manager.is_live_dry_run", return_value=True), \
                 patch("data_manager.is_dry_run_enhanced", return_value=True), \
                 patch("data_manager._reconcile_live_trade_sources", side_effect=lambda h: (h, False)):
                record_live_trade({"type": "BUY", "symbol": "PEPE/USDT", "usdt_amount": 100})
                history = load_live_trade_history()
                self.assertAlmostEqual(history["virtual_balance"], 4900.0)
                record_live_trade({"type": "SELL", "symbol": "PEPE/USDT", "usdt_received": 120, "pnl": 20})
                history = load_live_trade_history()
                self.assertAlmostEqual(history["virtual_balance"], 5020.0)
                trades = history["trades"]
                self.assertAlmostEqual(compute_sim_cash_from_trades(trades, 5000), 5020.0)

    def test_record_live_trade_updates_virtual_balance_plain_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "live_trade_history.json")
            cfg = {
                "trading_mode": "live",
                "initial_capital_usdt": 5000,
                "live": {"dry_run": True, "dry_run_enhanced": False, "simulated_balance_usdt": 5000},
            }
            with patch("data_manager.get_data_file", return_value=path), \
                 patch("data_manager.get_config", return_value=cfg), \
                 patch("data_manager.is_live_dry_run", return_value=True), \
                 patch("data_manager.is_dry_run_enhanced", return_value=False), \
                 patch("data_manager._reconcile_live_trade_sources", side_effect=lambda h: (h, False)):
                record_live_trade({"type": "BUY", "symbol": "SIREN/USDT", "usdt_amount": 250})
                history = load_live_trade_history()
                self.assertAlmostEqual(history["virtual_balance"], 4750.0)
                record_live_trade({"type": "SELL", "symbol": "SIREN/USDT", "usdt_received": 260, "pnl": 10})
                history = load_live_trade_history()
                self.assertAlmostEqual(history["virtual_balance"], 5010.0)
                self.assertAlmostEqual(history["realized_pnl"], 10.0)

    def test_risk_manager_approves_buy_with_simulated_balance(self):
        cfg = self._enhanced_config()
        rm = RiskManager(cfg)
        with patch("risk.risk_manager.load_live_trade_history", return_value={"virtual_balance": 5000.0, "trades": []}), \
             patch("risk.risk_manager.count_open_positions", return_value=0), \
             patch("risk.risk_manager.get_position", return_value={"amount": 0}), \
             patch.object(rm, "_daily_trades_count", return_value=0):
            decision = rm.evaluate(
                TradeOrder("BUY", "PEPE/USDT", 0.001, 0, usdt_amount=25),
                source="auto",
                trust_score=70,
                confidence=60,
                indicators={"rsi": 30, "atr_pct": 3, "vol_multiplier": 1.2, "lower_bb": 0},
            )
        self.assertTrue(decision.approved)
        self.assertGreater(decision.order.usdt_amount, 0)

    def test_fetch_usdt_balance_returns_simulated_in_enhanced_mode(self):
        cfg = self._enhanced_config()
        with patch("services.gate_balance.load_live_trade_history", return_value={"virtual_balance": 4321.0}):
            self.assertAlmostEqual(fetch_usdt_balance(cfg), 4321.0)

    def test_fetch_usdt_balance_returns_simulated_in_plain_dry_run(self):
        raw = dict(get_config())
        raw["trading_mode"] = "live"
        raw.setdefault("live", {})["dry_run"] = True
        raw["live"]["dry_run_enhanced"] = False
        raw["live"]["simulated_balance_usdt"] = 5000
        cfg = BotConfig()
        cfg._raw = raw
        with patch("services.gate_balance.load_live_trade_history", return_value={"virtual_balance": 3952.19}):
            self.assertAlmostEqual(fetch_usdt_balance(cfg), 3952.19)

    def test_risk_status_uses_simulated_ledger(self):
        cfg = self._enhanced_config()
        rm = RiskManager(cfg)
        with patch("risk.risk_manager.load_live_trade_history", return_value={"virtual_balance": 4800.0, "trades": []}), \
             patch("risk.risk_manager.count_open_positions", return_value=1), \
             patch.object(rm, "_daily_trades_count", return_value=2):
            summary = rm.status_summary()
        self.assertEqual(summary["ledger_source"], "simulated")
        self.assertEqual(summary["virtual_balance"], 4800.0)


if __name__ == "__main__":
    unittest.main()