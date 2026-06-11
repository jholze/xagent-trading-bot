import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.models import TradeOrder
from notifications.terminal_dashboard import (
    build_cycle_summary,
    format_recent_trade_line,
    recent_orders_lines,
    recent_trades_lines,
)
from services.order_service import OrderService


class TestCycleSummary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.scope_patch = patch("data_manager.ORDERS_SCOPE_FILES", {
            "demo": os.path.join(self.tmp.name, "orders.demo.json"),
            "paper": os.path.join(self.tmp.name, "orders.paper.json"),
            "live": os.path.join(self.tmp.name, "orders.live.json"),
        })
        self.scope_patch.start()
        self.scope = patch("services.order_service.resolve_ledger_scope", return_value="paper")
        self.scope.start()

    def tearDown(self):
        self.scope.stop()
        self.scope_patch.stop()

    def _history(self, trades):
        return {
            "virtual_balance": 3999,
            "realized_pnl": 0.1,
            "trades": trades,
        }

    def _seed_orders(self):
        svc = OrderService("paper")
        svc.create_from_request(
            TradeOrder("BUY", "ARIA/USDT", 0.05, 0, usdt_amount=50, source="manual"),
            status="filled",
            telegram_token="o1",
        )
        svc.update_status("o1", "filled", execution={"usdt": 50, "price": 0.05, "amount": 1000})
        svc.create_from_request(
            TradeOrder("SELL", "SOL/USDT", 70, 2, signal="SELL", source="manual"),
            status="filled",
            telegram_token="o2",
        )
        svc.update_status("o2", "filled", execution={"usdt": 140, "price": 70, "amount": 2}, pnl=0.1)

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
        self.assertIn("Orders (24h", summary)

    def test_recent_orders_from_ledger(self):
        self._seed_orders()
        lines = recent_orders_lines()
        self.assertEqual(len(lines), 2)
        combined = "\n".join(lines)
        self.assertIn("ARIA", combined)
        self.assertIn("SOL", combined)

    def test_build_cycle_summary_includes_ledger_orders(self):
        self._seed_orders()
        with patch("notifications.terminal_dashboard.load_trade_history", return_value=self._history([])):
            summary = build_cycle_summary(coin_results=[], trading_mode="paper")
        self.assertIn("ARIA", summary)
        self.assertIn("SOL", summary)
        self.assertIn("/orders", summary)

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
        with patch("notifications.terminal_dashboard.load_trade_history", return_value=self._history([])):
            summary = build_cycle_summary(coin_results=[], trading_mode="paper")
        self.assertIn("No auto-trades executed", summary)
        self.assertIn("/orders", summary)


if __name__ == "__main__":
    unittest.main()