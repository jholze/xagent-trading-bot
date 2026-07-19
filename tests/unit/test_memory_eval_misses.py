"""Unit tests for the three live eval misses: gross_loss, macro_fed, social_lc."""

from __future__ import annotations

import unittest

from intelligence.memory.models import CoinProfile, Lesson, MarketEvent, utc_now_iso
from intelligence.memory.reflector import reflect
from intelligence.memory.retriever import (
    _type_hints_for_query,
    similar_events,
)


class _MemStore:
    def __init__(self):
        self.events: dict[str, MarketEvent] = {}
        self.lessons: dict[str, Lesson] = {}
        self.profiles: list[CoinProfile] = []
        self.trades: list = []

    def upsert_event(self, event: MarketEvent) -> bool:
        self.events[event.event_id] = event
        return True

    def get_event(self, event_id: str):
        return self.events.get(event_id)

    def list_events(
        self,
        symbol=None,
        event_type=None,
        since_iso=None,
        limit=50,
    ):
        out = []
        for e in self.events.values():
            if event_type and e.event_type != event_type:
                continue
            if symbol:
                base = symbol.split("/")[0].upper()
                if not any(base in (s or "").upper() for s in (e.symbols or [])):
                    continue
            if since_iso and (e.timestamp or "") < since_iso:
                continue
            out.append(e)
        return out[: int(limit)]

    def list_trades(self, tenant_id="default", limit=500, **_):
        return list(self.trades)[: int(limit)]

    def list_profiles(self, tenant_id="default", limit=200):
        return list(self.profiles)[: int(limit)]

    def get_profile(self, symbol, ledger_scope=None, tenant_id="default"):
        for p in self.profiles:
            if p.symbol == symbol and (
                not ledger_scope or p.ledger_scope == ledger_scope
            ):
                return p
        return None

    def upsert_profile(self, prof) -> bool:
        self.profiles = [p for p in self.profiles if not (
            p.symbol == prof.symbol and p.ledger_scope == prof.ledger_scope
        )]
        self.profiles.append(prof)
        return True

    def upsert_lesson(self, lesson: Lesson) -> bool:
        self.lessons[lesson.lesson_id] = lesson
        return True

    def list_lessons(self, symbol=None, limit=50):
        rows = list(self.lessons.values())
        if symbol:
            rows = [L for L in rows if symbol in (L.symbols or [])]
        return rows[: int(limit)]


class TestTypeHints(unittest.TestCase):
    def test_macro_hints(self):
        hints = _type_hints_for_query("FOMC Fed rate decision macro calendar pre window")
        self.assertIn("macro_scheduled", hints)

    def test_lc_hints(self):
        hints = _type_hints_for_query("lunarcrush social spike sentiment extreme")
        self.assertIn("lc_sentiment_extreme", hints)
        self.assertIn("lc_social_spike", hints)

    def test_soft_block_hints(self):
        hints = _type_hints_for_query("sensor entry gross loss soft_block rebuy cooloff")
        self.assertIn("soft_block", hints)


class TestSimilarEventsHybrid(unittest.TestCase):
    def setUp(self):
        self.store = _MemStore()
        # Flood with irrelevant news so plain limit=80 would drown signal without type hints
        for i in range(40):
            self.store.upsert_event(
                MarketEvent(
                    event_id=f"news_{i}",
                    timestamp=utc_now_iso(),
                    event_type="news",
                    symbols=["BTC/USDT"],
                    description=f"Bitcoin mining hash rate headline {i}",
                    source="rss",
                )
            )
        self.store.upsert_event(
            MarketEvent(
                event_id="fomc1",
                timestamp="2020-01-01T00:00:00Z",  # older than news
                event_type="macro_scheduled",
                symbols=["BTC/USDT"],
                description="FOMC Fed rate decision macro calendar scheduled",
                source="macro",
            )
        )
        self.store.upsert_event(
            MarketEvent(
                event_id="lc1",
                timestamp="2020-01-01T00:00:00Z",
                event_type="lc_sentiment_extreme",
                symbols=["SOL/USDT"],
                description="LunarCrush lc_sentiment_extreme social sentiment extreme SOL",
                source="lc",
            )
        )
        self.store.upsert_event(
            MarketEvent(
                event_id="sb1",
                timestamp="2020-01-01T00:00:00Z",
                event_type="soft_block",
                symbols=["BDX/USDT"],
                description="sensor entry gross loss soft_block rebuy cooloff BDX",
                source="reflector",
            )
        )

    def test_macro_fed_surfaces(self):
        hits = similar_events(
            "FOMC Fed rate decision macro calendar pre window",
            symbol="BTC/USDT",
            k=8,
            store=self.store,
        )
        blob = " ".join(f"{e.event_type} {e.description}" for e in hits).lower()
        self.assertTrue(any(n in blob for n in ("fomc", "fed", "macro", "calendar")))

    def test_social_lc_surfaces(self):
        hits = similar_events(
            "lunarcrush social spike sentiment extreme",
            k=8,
            store=self.store,
        )
        blob = " ".join(f"{e.event_type} {e.description}" for e in hits).lower()
        self.assertTrue(any(n in blob for n in ("lc_", "lunar", "sentiment", "social", "spike")))

    def test_soft_block_surfaces(self):
        hits = similar_events(
            "sensor entry gross loss soft_block rebuy cooloff",
            k=8,
            store=self.store,
        )
        blob = " ".join(f"{e.event_type} {e.description}" for e in hits).lower()
        self.assertTrue(any(n in blob for n in ("soft_block", "gross", "loss", "sensor")))


class TestReflectSoftBlockLesson(unittest.TestCase):
    def test_soft_block_profile_emits_lesson_and_event(self):
        store = _MemStore()
        store.profiles.append(
            CoinProfile(
                symbol="BDX/USDT",
                ledger_scope="demo",
                entry_bias="soft_block",
                size_bias=0.5,
                rationale="gross_loss n=2 worst_usdt=-180.0 scope=sensor_only",
                features={"soft_block_scope": "sensor_only", "last_loss_source": "sensor"},
                sells_30d=2,
                trades_30d=3,
            )
        )
        from intelligence.memory.models import TradeMemory

        store.trades.append(
            TradeMemory(
                trade_id="t1",
                symbol="BDX/USDT",
                direction="sell",
                pnl_usdt=-180.0,
                ledger_scope="demo",
            )
        )
        out = reflect(store, ledger_scope="demo", min_samples=99)
        self.assertGreaterEqual(out["lessons"], 1)
        lesson_blob = " ".join(L.text for L in store.lessons.values()).lower()
        self.assertIn("soft_block", lesson_blob)
        self.assertIn("gross loss", lesson_blob)
        self.assertIn("sensor", lesson_blob)
        self.assertTrue(any(e.event_type == "soft_block" for e in store.events.values()))

    def test_weak_history_soft_block_not_fake_gross_loss(self):
        store = _MemStore()
        store.profiles.append(
            CoinProfile(
                symbol="MIX/USDT",
                ledger_scope="demo",
                entry_bias="soft_block",
                size_bias=0.65,
                rationale="weak history win_rate=30% n=5 pnl=-40.0",
                features={"soft_block_scope": "all"},
                sells_30d=5,
                trades_30d=8,
            )
        )
        from intelligence.memory.models import TradeMemory

        store.trades.append(
            TradeMemory(
                trade_id="t2",
                symbol="MIX/USDT",
                direction="sell",
                pnl_usdt=-10.0,
                ledger_scope="demo",
            )
        )
        reflect(store, ledger_scope="demo", min_samples=99)
        lesson_blob = " ".join(L.text for L in store.lessons.values()).lower()
        self.assertIn("soft_block", lesson_blob)
        self.assertNotIn("gross loss", lesson_blob)


if __name__ == "__main__":
    unittest.main()
