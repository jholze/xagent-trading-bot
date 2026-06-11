import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from notifications.telegram_commands.watchlist_commands import (
    format_buy_list_message,
    format_watchlist_message,
    resolve_coin_by_display_index,
)


class TestWatchlistCommands(unittest.TestCase):
    def test_watchlist_message_numbered(self):
        coins = [
            {"symbol": "ARIA/USDT", "name": "Aria AI", "active": True},
            {"symbol": "SOL/USDT", "name": "Solana", "active": True},
        ]
        msg = format_watchlist_message(coins)
        self.assertIn("<b>1.</b>", msg)
        self.assertIn("ARIA/USDT", msg)
        self.assertIn("<b>2.</b>", msg)
        self.assertIn("SOL/USDT", msg)

    def test_buy_list_matches_watchlist_order(self):
        coins = [
            {"symbol": "ARIA/USDT", "name": "Aria AI", "active": True},
            {"symbol": "SOL/USDT", "name": "Solana", "active": True},
        ]
        prices = {"ARIA/USDT": 0.05, "SOL/USDT": 145.0}
        msg = format_buy_list_message(coins, prices)
        self.assertIn("Coins kaufen", msg)
        self.assertIn("/buy NUMMER USDT", msg)
        self.assertLess(msg.index("ARIA"), msg.index("SOL"))
        picked = resolve_coin_by_display_index(coins, 1)
        self.assertEqual(picked["symbol"], "SOL/USDT")


if __name__ == "__main__":
    unittest.main()