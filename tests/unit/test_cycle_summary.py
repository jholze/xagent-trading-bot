import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from notifications.terminal_dashboard import (
    build_cycle_summary,
    format_recent_trade_line,
    recent_trades_lines,
)


class TestCycleSummary(unittest.TestCase):
    def _history(self, trades):
        return {
            "virtual_balance": 3999,
            "realized_pnl": 0.1,
            "trades": trades,
        }

    def test_build_cycle_summary_shows_auto_executed(self):
        summary = build_cycle_summary(
            coin_results=[{
                "symbol": "ARIA/USDT",
                "executed": True,
                "order_type": "BUY",
                "normalized_action": "BUY",
            }],
            trading_mode="paper",
            x_signal_count=2,
            cmc_signal_count=1,
        )
        self.assertIn("Cycle Summary", summary)
        self.assertIn("Auto-Executed", summary)
        self.assertIn("ARIA/USDT", summary)
        self.assertIn("Trades (24h, Ledger)", summary)

    def test_recent_trades_include_manual_and_auto(self):
        now = datetime.now().isoformat()
        old = (datetime.now() - timedelta(hours=30)).isoformat()
        history = self._history([
            {"type": "BUY", "symbol": "OLD/USDT", "usdt_amount": 10, "source": "auto", "timestamp": old},
            {"type": "BUY", "symbol": "ARIA/USDT", "usdt_amount": 50, "source": "manual", "timestamp": now},
            {"type": "BUY", "symbol": "SOL/USDT", "usdt_amount": 500, "source": "manual", "timestamp": now},
            {"type": "SELL", "symbol": "SOL/USDT", "usdt_received": 5, "pnl": 0.1, "source": "manual", "timestamp": now},
        ])
        with patch("notifications.terminal_dashboard.load_trade_history", return_value=history):
            summary = build_cycle_summary(coin_results=[], trading_mode="paper")
        self.assertIn("ARIA", summary)
        self.assertIn("SOL", summary)
        self.assertIn("manuell", summary)
        self.assertNotIn("OLD", summary)

    def test_format_recent_trade_line_labels_source(self):
        buy = format_recent_trade_line({
            "type": "BUY", "symbol": "ARIA/USDT", "usdt_amount": 200, "source": "manual",
        })
        sell = format_recent_trade_line({
            "type": "SELL", "symbol": "SOL/USDT", "usdt_received": 120, "pnl": 3.5, "source": "auto",
        })
        self.assertIn("manuell", buy)
        self.assertIn("200", buy)
        self.assertIn("Auto", sell)
        self.assertIn("PnL", sell)

    def test_recent_trades_empty_message(self):
        lines = recent_trades_lines({"trades": []})
        self.assertEqual(len(lines), 1)
        self.assertIn("Keine Trades", lines[0])

    def test_no_auto_executed_still_shows_ledger_hint(self):
        history = self._history([])
        with patch("notifications.terminal_dashboard.load_trade_history", return_value=history):
            summary = build_cycle_summary(coin_results=[], trading_mode="paper")
        self.assertIn("No auto-trades executed", summary)
        self.assertIn("Manuelle /buy", summary)


if __name__ == "__main__":
    unittest.main()