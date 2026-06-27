import unittest

from core.cycle_order import order_watchlist_positions_first


class TestCycleOrder(unittest.TestCase):
    def _coins(self, *symbols):
        return [{"symbol": s, "timeframe": "4h", "active": True} for s in symbols]

    def test_empty_positions_preserves_order(self):
        coins = self._coins("BTC/USDT", "ETH/USDT", "DOGE/USDT")
        self.assertEqual(order_watchlist_positions_first(coins, []), coins)

    def test_positions_moved_to_front(self):
        coins = self._coins("BTC/USDT", "ETH/USDT", "DOGE/USDT", "TRX/USDT")
        positions = [
            {"symbol": "DOGE/USDT", "timeframe": "1h"},
            {"symbol": "TRX/USDT", "timeframe": "4h"},
        ]
        ordered = order_watchlist_positions_first(coins, positions)
        self.assertEqual(
            [c["symbol"] for c in ordered],
            ["DOGE/USDT", "TRX/USDT", "BTC/USDT", "ETH/USDT"],
        )

    def test_position_not_in_watchlist_still_scanned(self):
        coins = self._coins("BTC/USDT", "ETH/USDT")
        positions = [{"symbol": "H/USDT", "timeframe": "1h"}]
        ordered = order_watchlist_positions_first(coins, positions)
        self.assertEqual(ordered[0]["symbol"], "H/USDT")
        self.assertEqual(ordered[0]["timeframe"], "1h")
        self.assertEqual(len(ordered), 3)

    def test_no_duplicate_symbols(self):
        coins = self._coins("DOGE/USDT", "ETH/USDT")
        positions = [{"symbol": "DOGE/USDT", "timeframe": "1h"}]
        ordered = order_watchlist_positions_first(coins, positions)
        symbols = [c["symbol"] for c in ordered]
        self.assertEqual(len(symbols), len(set(symbols)))


if __name__ == "__main__":
    unittest.main()