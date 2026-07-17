"""Trading Memory layer tests (Epic #30) — no live ledger writes."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from intelligence.memory.cache import get_size_bias, invalidate_cache
from intelligence.memory.embeddings import cosine, embed_text
from intelligence.memory.event_ingest import impact_from_text, ingest_news_item, ingest_regime_event
from intelligence.memory.models import CoinProfile, MarketEvent, TradeMemory
from intelligence.memory.rebuild import compute_profile_from_trades, orders_to_trade_memories
from intelligence.memory.reflector import reflect
from intelligence.memory.retriever import compact_context, similar_events
from intelligence.memory.store import InMemoryMemoryStore


class TestMemoryModelsStore(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryMemoryStore()
        invalidate_cache()

    def test_profile_roundtrip(self):
        p = CoinProfile(symbol="SOL/USDT", size_bias=0.7, rationale="test", sells_30d=5)
        self.assertTrue(self.store.upsert_profile(p))
        got = self.store.get_profile("SOL/USDT")
        self.assertIsNotNone(got)
        self.assertEqual(got.symbol, "SOL/USDT")
        self.assertAlmostEqual(got.size_bias, 0.7)

    def test_event_dedupe_id(self):
        e = MarketEvent(
            event_id="news:abc",
            timestamp="2026-07-17T12:00:00Z",
            event_type="news",
            description="BTC hack rumor",
            impact_score=-0.5,
            symbols=["BTC/USDT"],
        )
        self.store.upsert_event(e)
        self.assertEqual(self.store.get_event("news:abc").event_type, "news")

    def test_forbidden_collections_enforced(self):
        from intelligence.memory.store import MemoryStore

        with self.assertRaises(RuntimeError):
            MemoryStore()._col("orders")


class TestRebuild(unittest.TestCase):
    def test_orders_to_trades_and_profile(self):
        now = datetime.now(timezone.utc)
        ts = (now - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        orders = [
            {
                "id": "1",
                "status": "filled",
                "side": "buy",
                "symbol": "SOL/USDT",
                "source": "dca",
                "tenant_id": "default",
                "request": {"price": 100, "usdt": 150},
                "timestamps": {"filled": ts},
            },
            {
                "id": "2",
                "status": "filled",
                "side": "sell",
                "symbol": "SOL/USDT",
                "source": "auto",
                "tenant_id": "default",
                "pnl": -12.0,
                "request": {"price": 90, "usdt": 140},
                "timestamps": {"filled": ts},
            },
            {
                "id": "3",
                "status": "filled",
                "side": "sell",
                "symbol": "SOL/USDT",
                "pnl": -8.0,
                "request": {"usdt": 100},
                "timestamps": {"filled": ts},
            },
            {
                "id": "4",
                "status": "filled",
                "side": "sell",
                "symbol": "SOL/USDT",
                "pnl": -5.0,
                "request": {"usdt": 100},
                "timestamps": {"filled": ts},
            },
        ]
        trades = orders_to_trade_memories(orders, ledger_scope="live", tenant_id="default")
        self.assertGreaterEqual(len(trades), 4)
        sells = [t for t in trades if t.direction == "sell"]
        self.assertEqual(len(sells), 3)
        prof = compute_profile_from_trades(
            "SOL/USDT", trades, ledger_scope="live", tenant_id="default", min_samples=3
        )
        self.assertLess(prof.size_bias, 1.0)
        self.assertEqual(prof.entry_bias, "soft_block")
        self.assertIn("weak", prof.rationale.lower())


class TestEventIngest(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryMemoryStore()

    def test_impact_keywords(self):
        self.assertLess(impact_from_text("major exchange hack reported"), 0)
        self.assertGreater(impact_from_text("ETF approval breakthrough"), 0)

    def test_ingest_news_and_regime(self):
        ev = ingest_news_item(
            title="SEC charges crypto exchange",
            url="https://example.com/a",
            source="test",
            store=self.store,
        )
        self.assertIsNotNone(ev)
        self.assertEqual(ev.event_type, "news")
        self.assertLess(ev.impact_score, 0)
        # second call same url → same id
        ev2 = ingest_news_item(
            title="SEC charges crypto exchange",
            url="https://example.com/a",
            source="test",
            store=self.store,
        )
        self.assertEqual(ev.event_id, ev2.event_id)
        r = ingest_regime_event(
            source="oracle", regime="RISK_OFF", size_mult=0.35, store=self.store
        )
        self.assertEqual(r.event_type, "regime_change")


class TestReflectRetrieve(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryMemoryStore()
        # seed weak history
        ts = "2026-07-10T12:00:00Z"
        for i, pnl in enumerate([-10, -8, -12, 2]):
            self.store.upsert_trade(
                TradeMemory(
                    trade_id=f"t{i}",
                    symbol="PEPE/USDT",
                    entry_time=ts,
                    direction="sell",
                    pnl_usdt=pnl,
                    outcome="loss" if pnl < 0 else "win",
                    tenant_id="default",
                )
            )
        self.store.upsert_profile(
            CoinProfile(symbol="PEPE/USDT", size_bias=1.0, rationale="init")
        )

    def test_reflect_creates_lesson(self):
        out = reflect(self.store, min_samples=3)
        self.assertGreaterEqual(out["lessons"], 1)
        les = self.store.list_lessons(symbol="PEPE/USDT")
        self.assertTrue(les)
        prof = self.store.get_profile("PEPE/USDT")
        self.assertLessEqual(prof.size_bias, 0.7)

    def test_similar_events_local(self):
        self.store.upsert_event(
            MarketEvent(
                event_id="e1",
                timestamp="2026-07-17T00:00:00Z",
                event_type="news",
                description="exchange hack causes crash",
                impact_score=-0.8,
                symbols=["BTC/USDT"],
                embedding=embed_text("news exchange hack causes crash"),
            )
        )
        hits = similar_events("hack exploit exchange", store=self.store, k=3)
        self.assertTrue(hits)
        self.assertEqual(hits[0].event_id, "e1")

    def test_size_bias_fail_open(self):
        invalidate_cache()
        # no store wiring — default MemoryStore may fail → 1.0
        b = get_size_bias("NOEXIST/USDT")
        self.assertEqual(b, 1.0)


class TestEmbeddings(unittest.TestCase):
    def test_cosine_self(self):
        v = embed_text("bitcoin etf approval")
        self.assertAlmostEqual(cosine(v, v), 1.0, places=5)
        self.assertGreater(cosine(v, embed_text("bitcoin etf news")), 0.1)


if __name__ == "__main__":
    unittest.main()
