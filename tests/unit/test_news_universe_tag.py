"""News/event tagging for book+watchlist universe."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from intelligence.memory.event_ingest import (
    extract_symbols,
    impact_from_text,
    ingest_news_item,
    match_universe_symbols,
)


class TestMatchUniverse(unittest.TestCase):
    def test_matches_book_ticker(self):
        uni = ["ETH/USDT", "LAB/USDT", "SOL/USDT"]
        hits = match_universe_symbols("LAB protocol unlock dumps 20%", uni)
        self.assertIn("LAB/USDT", hits)

    def test_prefers_universe_over_generic(self):
        uni = ["BDX/USDT", "HYPE/USDT"]
        hits = match_universe_symbols("HYPE listing on major exchange sparks rally", uni)
        self.assertIn("HYPE/USDT", hits)

    def test_extract_still_works(self):
        syms = extract_symbols("Bitcoin and ETH rise as ETF flows return")
        self.assertTrue(any("ETH" in s for s in syms) or "BTC/USDT" in syms)


class TestIngestNewsClassification(unittest.TestCase):
    def test_unlock_headline_typed(self):
        store = MagicMock()
        store.get_event.return_value = None
        store.upsert_event.return_value = True

        ev = ingest_news_item(
            title="ARB token unlock cliff hits markets",
            body="Large vesting unlock this week",
            source="test",
            store=store,
            universe=["ARB/USDT", "ETH/USDT"],
        )
        self.assertIsNotNone(ev)
        self.assertEqual(ev.event_type, "token_unlock")
        self.assertIn("ARB/USDT", ev.symbols)
        store.upsert_event.assert_called()

    def test_impact_negative_hack(self):
        self.assertLess(impact_from_text("protocol hack and exploit drain"), 0)


if __name__ == "__main__":
    unittest.main()
