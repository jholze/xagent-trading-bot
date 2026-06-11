import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from notifications.telegram_commands.position_display import (
    format_portfolio_summary,
    format_position_card,
    format_positions_message,
    format_sell_list_message,
    format_trade_banner,
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
            "average_entry": 0.0442,
            "sold_percent": 0.3,
            "last_action": "SELL",
        }
        card = format_position_card(1, p, 0.0389, numbered=True)
        self.assertIn("ARIA", card)
        self.assertIn("880.0000", card)
        self.assertIn("0.0389", card)
        self.assertIn("Bereits verkauft", card)
        self.assertIn("Letzte Aktion", card)

    def test_portfolio_summary_german_labels(self):
        msg = format_portfolio_summary(
            {"virtual_balance": 4911, "realized_pnl": 12.5},
            total_unreal=25.0,
            position_count=2,
            mode_label="paper (local ledger)",
        )
        self.assertIn("Gesamtwert", msg)
        self.assertIn("Gesamt-PnL", msg)
        self.assertIn("Positionen (2)", msg)

    def test_empty_positions_message(self):
        msg = format_positions_message([], {}, {"virtual_balance": 5000})
        self.assertIn("Keine offenen Positionen", msg)

    def test_sell_list_includes_command_hint(self):
        active = [{"symbol": "ARIA/USDT", "amount": 100, "average_entry": 0.04, "sold_percent": 0}]
        msg = format_sell_list_message(active, {"ARIA/USDT": 0.05})
        self.assertIn("/sell NUMMER PROZENT", msg)
        self.assertIn("1.", msg)

    def test_positions_sorted_by_value(self):
        active = [
            {"symbol": "SMALL/USDT", "amount": 10, "average_entry": 1.0, "sold_percent": 0},
            {"symbol": "BIG/USDT", "amount": 100, "average_entry": 1.0, "sold_percent": 0},
        ]
        msg = format_positions_message(active, {"SMALL/USDT": 1.0, "BIG/USDT": 1.0}, {"virtual_balance": 1000, "trades": []})
        self.assertLess(msg.index("BIG"), msg.index("SMALL"))

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

    def test_send_positions_snapshot_includes_trade_banner(self):
        result = TradeResult(True, "BUY", "ARIA/USDT", amount=50, price=0.04, usdt_amount=2)
        with patch("telegram_notifier.send_telegram_message") as mock_send, \
             patch("price_fetcher.get_prices_batch", return_value={}), \
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