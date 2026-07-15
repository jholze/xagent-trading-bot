"""Demo + live dry-run must read/write the same live scope trade history."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.config import BotConfig
from data_manager import load_live_trade_history, record_live_trade
from notifications.telegram_commands.position_display import load_trade_history_safe


class TestDemoLiveTradeHistory(unittest.TestCase):
    def _demo_live_cfg(self) -> dict:
        return {
            "trading_mode": "live",
            "live": {
                "dry_run": True,
                "dry_run_enhanced": True,
                "simulated_balance_usdt": 100_000.0,
            },
            "architecture": {"ledger_backend": "local"},
        }

    @patch("data_manager.is_demo_mode", return_value=True)
    @patch("data_manager.is_live_dry_run", return_value=True)
    @patch("data_manager.load_trade_history_document")
    @patch("data_manager.save_trade_history_document")
    def test_record_live_trade_uses_live_scope_in_demo(
        self,
        mock_save,
        mock_load_doc,
        _dry,
        _demo,
    ):
        live_doc = {
            "virtual_balance": 100_000.0,
            "trades": [],
            "realized_pnl": 0.0,
        }
        mock_load_doc.return_value = dict(live_doc)

        def _capture_save(data, scope, **kwargs):
            self.assertEqual(scope, "live")
            live_doc.update(data)
            return True

        mock_save.side_effect = _capture_save

        record_live_trade(
            {
                "type": "BUY",
                "symbol": "XPL/USDT",
                "usdt_amount": 3025.0,
                "timestamp": "2026-07-15T08:43:51",
            }
        )
        mock_load_doc.assert_called_with("live")
        self.assertEqual(len(live_doc["trades"]), 1)

    @patch("data_manager.is_demo_mode", return_value=True)
    @patch("data_manager.uses_simulated_live_portfolio", return_value=True)
    @patch("data_manager.load_live_trade_history")
    def test_portfolio_snapshot_uses_live_history(self, mock_live, _sim, _demo):
        mock_live.return_value = {"virtual_balance": 95_000.0, "trades": []}
        history = load_trade_history_safe()
        self.assertEqual(history["virtual_balance"], 95_000.0)
        mock_live.assert_called_once()


if __name__ == "__main__":
    unittest.main()