"""Watchlist Telegram messages must stay under the 4096 char limit."""

from __future__ import annotations

import unittest

from notifications.telegram_commands.watchlist_commands import (
    _WATCHLIST_CHUNK_LIMIT,
    chunk_watchlist_messages,
    format_watchlist_message,
)


def _fake_coins(n: int) -> list[dict]:
    out = []
    for i in range(n):
        out.append({
            "symbol": f"COIN{i}/USDT",
            "name": f"Very Long Coin Name Number {i} For HTML Links",
            "active": True,
            "source": "cmc_trending" if i % 3 == 0 else "manual",
            "trending_rank": i + 1 if i % 3 == 0 else None,
        })
    return out


class TestWatchlistChunk(unittest.TestCase):
    def test_chunks_under_limit_for_large_list(self):
        coins = _fake_coins(60)
        parts = chunk_watchlist_messages(coins)
        self.assertGreaterEqual(len(parts), 2)
        for p in parts:
            self.assertLessEqual(len(p), 4096, msg=f"chunk len={len(p)}")
            self.assertLessEqual(len(p), _WATCHLIST_CHUNK_LIMIT + 40)  # page tag room

    def test_empty(self):
        parts = chunk_watchlist_messages([])
        self.assertEqual(len(parts), 1)
        self.assertIn("leer", parts[0].lower())

    def test_format_joins_chunks(self):
        coins = _fake_coins(5)
        msg = format_watchlist_message(coins)
        self.assertIn("Watchlist", msg)
        self.assertIn("COIN0", msg)


if __name__ == "__main__":
    unittest.main()
