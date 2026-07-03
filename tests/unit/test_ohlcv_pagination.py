import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import historical_prices as hp


def _bar(ts_ms: int, price: float = 1.0) -> list:
    return [ts_ms, price, price + 0.1, price - 0.1, price, 100.0]


class TestOhlcvPagination(unittest.TestCase):
    def setUp(self):
        hp.clear_cache()

    def test_cache_key_includes_window_bounds(self):
        start = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
        end = start + timedelta(hours=10)
        with patch.object(hp, "_gate_exchange") as mock_ex:
            mock_ex.return_value.fetch_ohlcv.return_value = [_bar(int(start.timestamp() * 1000))]
            hp._fetch_ohlcv_range("BTC/USDT", start, end, "1h")
            hp._fetch_ohlcv_range("BTC/USDT", start, end + timedelta(hours=5), "1h")
            self.assertEqual(mock_ex.return_value.fetch_ohlcv.call_count, 2)

    def test_pagination_merges_multiple_chunks(self):
        start = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
        end = start + timedelta(hours=1500)
        first_since = int(start.timestamp() * 1000)
        chunk1 = [_bar(first_since + i * 3_600_000, 1.0 + i * 0.01) for i in range(1000)]
        chunk2 = [_bar(chunk1[-1][0] + 3_600_000 + i * 3_600_000, 11.0) for i in range(200)]

        def fake_fetch(symbol, timeframe, since=None, limit=None):
            if since == first_since:
                return chunk1
            if since == chunk1[-1][0] + 3_600_000:
                return chunk2
            return []

        with patch.object(hp, "_gate_exchange") as mock_ex:
            mock_ex.return_value.fetch_ohlcv.side_effect = fake_fetch
            bars = hp._fetch_ohlcv_range("BTC/USDT", start, end, "1h")
        self.assertEqual(len(bars), 1200)
        self.assertEqual(bars[0][0], chunk1[0][0])
        self.assertEqual(bars[-1][0], chunk2[-1][0])


if __name__ == "__main__":
    unittest.main()