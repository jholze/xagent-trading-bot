import unittest

import pandas as pd

from services.market_service import MarketService


class TestMarketService24hMetrics(unittest.TestCase):
    def test_compute_24h_range_and_change(self):
        rows = []
        for i in range(30):
            close = 100.0 - i * 0.5
            rows.append({
                "ts": i,
                "open": close,
                "high": close + 2.0,
                "low": close - 2.0,
                "close": close,
                "volume": 1000.0,
            })
        df = pd.DataFrame(rows)
        range_pct, change_pct = MarketService._compute_24h_metrics(df, "1h")
        self.assertIsNotNone(range_pct)
        self.assertIsNotNone(change_pct)
        self.assertGreater(range_pct, 10.0)
        self.assertLess(change_pct, 0.0)