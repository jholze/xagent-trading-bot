import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from notifications.telegram_commands.position_display import (
    _trade_line,
    format_portfolio_summary,
    format_position_card,
    format_positions_message,
    format_sell_list_message,
    format_trade_banner,
    resolve_portfolio_context,
    resolve_position_by_display_index,
    send_positions_snapshot,
    sort_positions_by_value,
)
from core.models import TradeResult


class TestPositionDisplay(unittest.TestCase):
    def test_position_card_shows_key_fields(self):
        p = {
            "symbol": "ARIA/USDT",
            "amount": 880.0,
            "peak_amount": 1257.0,
            "average_entry": 0.0442,
            "sold_percent": 0.3,
            "last_action": "SELL",
        }
        card = format_position_card(1, p, 0.0389, numbered=True)
        self.assertIn("ARIA", card)
        self.assertIn("880.0000", card)
        self.assertIn("0.0389", card)
        self.assertIn("Bereits verkauft", card)
        self.assertIn("30%", card)
        self.assertIn("Letzte Aktion", card)

    def test_portfolio_summary_german_labels(self):
        msg = format_portfolio_summary(
            {"virtual_balance": 4911, "realized_pnl": 12.5},
            total_unreal=25.0,
            position_count=2,
            mode_label="paper (local ledger)",
            positions_market_value=89.0,
        )
        self.assertIn("Gesamtwert", msg)
        self.assertIn("$5,000", msg)
        self.assertIn("Gesamt-PnL", msg)
        self.assertIn("Positionen (2)", msg)

    def test_portfolio_summary_total_value_uses_position_market_not_unreal_only(self):
        msg = format_portfolio_summary(
            {"virtual_balance": 3952.19, "realized_pnl": -111.82},
            total_unreal=18.5,
            position_count=4,
            cash_balance=3952.19,
            cash_label="Cash (Dry Run)",
            positions_market_value=1111.82,
        )
        self.assertIn("$5,064", msg)

    def test_resolve_portfolio_context_live_dry_run_without_enhanced(self):
        cfg = type("Cfg", (), {
            "raw": {"trading_mode": "live", "live": {"dry_run": True, "dry_run_enhanced": False}},
            "trading_mode": "live",
            "simulated_balance_usdt": 5000,
        })()
        with patch("notifications.telegram_commands.position_display.get_bot_config", return_value=cfg), \
             patch("notifications.telegram_commands.position_display.load_trade_history_safe", return_value={
                 "virtual_balance": 3952.19, "realized_pnl": -111.82, "trades": [],
             }), \
             patch("notifications.telegram_commands.position_display.is_dry_run_enhanced", return_value=False), \
             patch("notifications.telegram_commands.position_display.fetch_usdt_balance") as mock_gate:
            ctx = resolve_portfolio_context()
            mock_gate.assert_not_called()
        self.assertEqual(ctx["cash_label"], "Cash (Dry Run)")
        self.assertAlmostEqual(ctx["cash_balance"], 3952.19)
        self.assertIsNone(ctx["gate_holdings"])

    def test_empty_positions_message(self):
        msg = format_positions_message([], {}, {"virtual_balance": 5000})
        self.assertIn("Keine offenen Positionen", msg)

    def test_sell_list_includes_command_hint(self):
        active = [{"symbol": "ARIA/USDT", "amount": 100, "average_entry": 0.04, "sold_percent": 0}]
        msg = format_sell_list_message(active, {"ARIA/USDT": 0.05})
        self.assertIn("1 30", msg)
        self.assertIn("Danach nur noch", msg)
        self.assertIn("1.", msg)

    def test_positions_sorted_by_value(self):
        active = [
            {"symbol": "SMALL/USDT", "amount": 10, "average_entry": 1.0, "sold_percent": 0},
            {"symbol": "BIG/USDT", "amount": 100, "average_entry": 1.0, "sold_percent": 0},
        ]
        msg = format_positions_message(active, {"SMALL/USDT": 1.0, "BIG/USDT": 1.0}, {"virtual_balance": 1000, "trades": []})
        self.assertLess(msg.index("BIG"), msg.index("SMALL"))

    def test_positions_message_shows_stable_numbers(self):
        active = [
            {"symbol": "SOL/USDT", "amount": 10, "average_entry": 1.0, "sold_percent": 0},
            {"symbol": "BTC/USDT", "amount": 1, "average_entry": 1.0, "sold_percent": 0},
        ]
        prices = {"SOL/USDT": 10.0, "BTC/USDT": 1000.0}
        positions_msg = format_positions_message(active, prices, {"virtual_balance": 1000, "trades": []})
        sell_msg = format_sell_list_message(active, prices)
        self.assertIn("<b>1.</b>", positions_msg)
        self.assertIn("<b>2.</b>", positions_msg)
        self.assertLess(positions_msg.index("BTC"), positions_msg.index("SOL"))
        self.assertLess(sell_msg.index("BTC"), sell_msg.index("SOL"))

    def test_sell_index_matches_display_order(self):
        """Display #2 must resolve to second-highest value, not raw list order."""
        active = [
            {"symbol": "XRP/USDT", "amount": 50, "average_entry": 1.0, "sold_percent": 0},
            {"symbol": "SOL/USDT", "amount": 10, "average_entry": 1.0, "sold_percent": 0},
            {"symbol": "BTC/USDT", "amount": 1, "average_entry": 1.0, "sold_percent": 0},
        ]
        prices = {"XRP/USDT": 1.0, "SOL/USDT": 10.0, "BTC/USDT": 1000.0}
        sorted_active = sort_positions_by_value(active, prices)
        self.assertEqual(sorted_active[0]["symbol"], "BTC/USDT")
        self.assertEqual(sorted_active[1]["symbol"], "SOL/USDT")
        self.assertEqual(sorted_active[2]["symbol"], "XRP/USDT")
        # /sell 2 → index 1 → SOL (not XRP from unsorted list)
        picked = resolve_position_by_display_index(active, prices, 1)
        self.assertEqual(picked["symbol"], "SOL/USDT")

    def test_trade_banner_buy_and_sell(self):
        buy = TradeResult(True, "BUY", "ARIA/USDT", amount=100, price=0.04, usdt_amount=4)
        sell = TradeResult(True, "SELL", "ARIA/USDT", amount=30, price=0.05, usdt_amount=1.5, pnl=0.3)
        self.assertIn("Kauf ausgeführt", format_trade_banner(buy))
        self.assertIn("Verkauf ausgeführt", format_trade_banner(sell))
        self.assertIn("PnL", format_trade_banner(sell))

    def test_trade_line_shows_manual_source(self):
        line = _trade_line({
            "type": "BUY", "symbol": "CAT/USDT", "amount": 100, "price": 0.0000015,
            "source": "manual", "timestamp": "2026-06-12T15:35:11",
        })
        self.assertIn("Manuell", line)

    def test_trade_line_buy_shows_usdt_when_amount_missing(self):
        line = _trade_line({
            "type": "BUY", "symbol": "AARK/USDT", "amount": 0, "usdt_amount": 37.5,
            "price": 0.0011504, "source": "auto", "timestamp": "2026-06-15T20:20:26",
        })
        self.assertIn("$38", line)
        self.assertNotIn("0.0000", line)

    def test_resolve_portfolio_context_dry_run_uses_sim_cash(self):
        cfg = type("Cfg", (), {"raw": {"trading_mode": "live", "live": {"dry_run": True, "dry_run_enhanced": True}}})()
        with patch("notifications.telegram_commands.position_display.get_bot_config", return_value=cfg), \
             patch("notifications.telegram_commands.position_display.load_trade_history_safe", return_value={
                 "virtual_balance": 3904.25, "total_pnl": -10, "trades": [],
             }), \
             patch("notifications.telegram_commands.position_display.is_dry_run_enhanced", return_value=True):
            ctx = resolve_portfolio_context()
        self.assertEqual(ctx["cash_label"], "Cash (Sim)")
        self.assertAlmostEqual(ctx["cash_balance"], 3904.25)
        self.assertIsNone(ctx["gate_holdings"])

    def test_format_position_card_shows_micro_cap_price(self):
        p = {
            "symbol": "CAT/USDT",
            "amount": 330250990.0,
            "average_entry": 1.514e-06,
            "sold_percent": 0,
        }
        msg = format_position_card(1, p, 1.514e-06, numbered=True, price_source="live")
        self.assertIn("CAT", msg)
        self.assertIn("$0.000001514", msg)
        self.assertNotIn("@ $0.00000151 ", msg)

    def test_format_position_card_marks_missing_price(self):
        p = {
            "symbol": "CAT/USDT",
            "amount": 100.0,
            "average_entry": 1.514e-06,
            "sold_percent": 0,
        }
        msg = format_position_card(1, p, 0.0, numbered=True, price_source="missing")
        self.assertIn("Kein Live-Kurs", msg)

    def test_send_positions_snapshot_includes_trade_banner(self):
        result = TradeResult(True, "BUY", "ARIA/USDT", amount=50, price=0.04, usdt_amount=2)
        with patch("telegram_notifier.send_telegram_message") as mock_send, \
             patch("price_fetcher.get_prices_batch", return_value=({}, {})), \
             patch("strategies.positions.list_active_positions", return_value=[]), \
             patch("data_manager.load_trade_history", return_value={"virtual_balance": 5000, "trades": []}), \
             patch("services.trading_service.TradingService") as mock_svc:
            mock_svc.return_value.mode_label.return_value = "paper"
            send_positions_snapshot(trade_result=result)
            msg = mock_send.call_args[0][0]
            self.assertIn("Kauf ausgeführt", msg)
            self.assertIn("Letzte Trades", msg)


if __name__ == "__main__":
    unittest.main()