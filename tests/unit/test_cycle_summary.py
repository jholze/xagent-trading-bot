import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
import unittest.mock
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.models import TradeOrder
from notifications.terminal_dashboard import (
    build_cycle_summary,
    format_executed_cycle_line,
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
        self.scope = patch("services.order_service.resolve_tenant_scope", return_value="paper")
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
                "usdt_amount": 250,
            }],
            trading_mode="paper",
            x_signal_count=2,
            cmc_signal_count=1,
            style="full",
        )
        self.assertIn("Zyklus-Zusammenfassung", summary)
        self.assertIn("Ausgeführt", summary)
        self.assertIn("ARIA", summary)
        self.assertIn("$250", summary)
        self.assertIn("Orders (24h", summary)

    def test_build_cycle_summary_compact_is_short(self):
        summary = build_cycle_summary(
            coin_results=[{
                "symbol": "ARIA/USDT",
                "executed": True,
                "order_type": "BUY",
                "usdt_amount": 250,
            }],
            trading_mode="paper",
            style="compact",
        )
        self.assertIn("Zyklus", summary)
        self.assertIn("NAV", summary)
        self.assertIn("ARIA", summary)
        self.assertIn("$250", summary)
        self.assertNotIn("Zyklus-Zusammenfassung", summary)
        self.assertNotIn("Entscheidungen:", summary)
        self.assertLess(len(summary), 900)

    def test_format_executed_cycle_line_includes_pnl(self):
        line = format_executed_cycle_line({
            "symbol": "SOL/USDT",
            "order_type": "SELL",
            "usdt_amount": 140,
            "pnl": 3.5,
        })
        self.assertIn("SOL", line)
        self.assertIn("SELL", line)
        self.assertIn("$140", line)
        self.assertIn("PnL", line)
        self.assertIn("+3.5", line)

    def test_build_cycle_summary_ledger_sell_shows_pnl(self):
        self._seed_orders()
        with patch("notifications.terminal_dashboard.load_trade_history", return_value=self._history([])):
            summary = build_cycle_summary(coin_results=[], trading_mode="paper", style="full")
        self.assertIn("PnL", summary)
        self.assertIn("SOL", summary)

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
            summary = build_cycle_summary(coin_results=[], trading_mode="paper", style="full")
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
        buy_with_pnl = format_recent_trade_line({
            "type": "BUY", "symbol": "ETH/USDT", "usdt_amount": 100, "pnl": 0.0, "source": "auto",
        })
        self.assertIn("manuell", buy)
        self.assertIn("200", buy)
        self.assertIn("Auto", sell)
        self.assertIn("PnL", sell)
        self.assertIn("+3.5", sell)
        self.assertIn("PnL", buy_with_pnl)

    def test_recent_trades_empty_message(self):
        lines = recent_trades_lines({"trades": []})
        self.assertEqual(len(lines), 1)
        self.assertIn("Keine Trades", lines[0])

    def test_no_auto_executed_still_shows_ledger_hint(self):
        with patch("notifications.terminal_dashboard.load_trade_history", return_value=self._history([])):
            summary = build_cycle_summary(coin_results=[], trading_mode="paper", style="full")
        self.assertIn("Keine Auto-Trades in diesem Zyklus", summary)
        self.assertIn("/orders", summary)

    def test_build_cycle_summary_shows_simulated_balance(self):
        live_hist = {"virtual_balance": 4750.0, "total_pnl": 12.5, "trades": []}
        mock_cfg = unittest.mock.MagicMock()
        mock_cfg.raw = {
            "trading_mode": "live",
            "live": {"dry_run": True, "dry_run_enhanced": True},
        }
        mock_cfg.trading_mode = "live"
        mock_cfg.simulated_balance_usdt = 5000
        with patch("notifications.terminal_dashboard.list_active_positions", return_value=[]), \
             patch("notifications.telegram_commands.position_display.load_trade_history_safe", return_value=live_hist), \
             patch("notifications.telegram_commands.position_display._refresh_positions_for_snapshot"), \
             patch("core.simulated_trading.uses_order_ledger_cash", return_value=True), \
             patch("data_manager.resolve_sim_cash_balance", return_value=4750.0), \
             patch("data_manager.resolve_sim_realized_pnl", return_value=12.5), \
             patch("notifications.terminal_dashboard.get_bot_config", return_value=mock_cfg):
            summary = build_cycle_summary(coin_results=[], trading_mode="live", style="full")
        self.assertIn("Sim USDT", summary)
        self.assertIn("4,750", summary)
        self.assertIn("Gesamtwert", summary)

    def test_build_cycle_summary_demo_uses_demo_cash_not_live(self):
        demo_hist = {"virtual_balance": 3648.0, "realized_pnl": 250.0, "trades": []}
        mock_cfg = unittest.mock.MagicMock()
        mock_cfg.raw = {
            "trading_mode": "live",
            "live": {"dry_run": True, "dry_run_enhanced": True},
        }
        mock_cfg.trading_mode = "live"
        with patch("notifications.terminal_dashboard.list_active_positions", return_value=[]), \
             patch("notifications.telegram_commands.position_display.load_trade_history_safe", return_value=demo_hist), \
             patch("notifications.telegram_commands.position_display._refresh_positions_for_snapshot"), \
             patch("core.simulated_trading.is_simulated_trading", return_value=True), \
             patch("core.simulated_trading.uses_order_ledger_cash", return_value=True), \
             patch("data_manager.resolve_sim_cash_balance", return_value=10_512.0), \
             patch("data_manager.resolve_sim_realized_pnl", return_value=250.0), \
             patch("notifications.terminal_dashboard.get_bot_config", return_value=mock_cfg), \
             patch("notifications.daily_portfolio.today_activity_stats", return_value=(0, 0, 0.0, False)):
            summary = build_cycle_summary(coin_results=[], trading_mode="live", style="full")
        self.assertIn("Sim USDT: $10,512", summary)
        self.assertNotIn("3,648", summary)
        self.assertNotIn("88,406", summary)

    def test_build_cycle_summary_live_dry_run_without_enhanced(self):
        live_hist = {"virtual_balance": 3952.19, "realized_pnl": -111.82, "trades": []}
        mock_cfg = unittest.mock.MagicMock()
        mock_cfg.raw = {
            "trading_mode": "live",
            "live": {"dry_run": True, "dry_run_enhanced": False},
        }
        mock_cfg.trading_mode = "live"
        mock_cfg.simulated_balance_usdt = 5000
        with patch("notifications.terminal_dashboard.list_active_positions", return_value=[]), \
             patch("notifications.terminal_dashboard._portfolio_snapshot", return_value={
                 "history": live_hist,
                 "balance": 3952.19,
                 "balance_label": "Sim USDT",
                 "realized": -111.82,
                 "unrealized": 0.0,
                 "total_value": 3952.19,
                 "ledger_scope": "demo",
             }), \
             patch("data_manager.is_dry_run_enhanced", return_value=False), \
             patch("notifications.terminal_dashboard.get_bot_config", return_value=mock_cfg):
            summary = build_cycle_summary(coin_results=[], trading_mode="live", style="full")
        self.assertIn("Sim USDT", summary)
        self.assertIn("3,952", summary)
        self.assertIn("-111.8", summary)

    def test_build_cycle_summary_total_value_includes_positions(self):
        live_hist = {"virtual_balance": 1000.0, "realized_pnl": 5.0, "trades": []}
        mock_cfg = unittest.mock.MagicMock()
        mock_cfg.raw = {
            "trading_mode": "live",
            "live": {"dry_run": True, "dry_run_enhanced": False},
        }
        mock_cfg.trading_mode = "live"
        mock_cfg.simulated_balance_usdt = 5000
        positions = [{
            "symbol": "ARIA/USDT",
            "amount": 100.0,
            "average_entry": 0.5,
        }]
        with patch("notifications.terminal_dashboard.list_active_positions", return_value=positions), \
             patch("price_fetcher.get_prices_batch", return_value={"ARIA/USDT": 2.0}), \
             patch("notifications.telegram_commands.position_display.load_trade_history_safe", return_value=live_hist), \
             patch(
                 "notifications.telegram_commands.position_display._refresh_positions_for_snapshot",
                 return_value=positions,
             ), \
             patch("core.simulated_trading.uses_order_ledger_cash", return_value=True), \
             patch("data_manager.resolve_sim_cash_balance", return_value=1000.0), \
             patch("data_manager.resolve_sim_realized_pnl", return_value=5.0), \
             patch("notifications.terminal_dashboard.get_bot_config", return_value=mock_cfg):
            summary = build_cycle_summary(coin_results=[], trading_mode="live", style="full")
        self.assertIn("Gesamtwert: $1,200", summary)


if __name__ == "__main__":
    unittest.main()