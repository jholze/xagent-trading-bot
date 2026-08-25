"""Unit tests for intelligence.memory.market_pulse (no Mongo, no network)."""

from __future__ import annotations

import os
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from intelligence.memory.market_pulse import (
    get_cached_market_pulse,
    market_pulse_score,
)
from intelligence.memory.models import MarketEvent


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeStore:
    """Canned list_events — optionally raises to exercise fail-open."""

    def __init__(self, events=None, *, boom: bool = False):
        self.events = list(events or [])
        self.boom = boom
        self.calls: list[dict] = []

    def list_events(self, *, symbol=None, event_type=None, since_iso=None, limit=50):
        self.calls.append(
            {
                "symbol": symbol,
                "event_type": event_type,
                "since_iso": since_iso,
                "limit": limit,
            }
        )
        if self.boom:
            raise RuntimeError("mongo down")
        out = []
        for e in self.events:
            et = e.event_type if hasattr(e, "event_type") else e.get("event_type")
            ts = e.timestamp if hasattr(e, "timestamp") else e.get("timestamp")
            if event_type and et != event_type:
                continue
            if since_iso and (ts or "") < since_iso:
                continue
            out.append(e)
        return out[:limit]


def _ev(
    *,
    event_id: str,
    minutes_ago: float,
    impact: float,
    event_type: str = "macro_news",
    description: str = "test event",
) -> MarketEvent:
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return MarketEvent(
        event_id=event_id,
        timestamp=_iso(ts),
        event_type=event_type,
        impact_score=impact,
        description=description,
        source="test",
        symbols=[],
    )


class TestMarketPulseScore(unittest.TestCase):
    def test_no_events_gives_zero_score_and_confidence(self):
        out = market_pulse_score(since_minutes=30, store=FakeStore([]))
        self.assertEqual(out["bearish_score"], 0.0)
        self.assertEqual(out["confidence"], 0.0)
        self.assertEqual(out["event_count"], 0)
        self.assertEqual(out["top_events"], [])

    def test_recent_strongly_bearish_macro_news_high_score(self):
        events = [
            _ev(event_id="n1", minutes_ago=1, impact=-1.0, description="crash"),
            _ev(event_id="n2", minutes_ago=2, impact=-0.9, description="hack"),
            _ev(event_id="n3", minutes_ago=3, impact=-0.8, description="sec charge"),
        ]
        out = market_pulse_score(since_minutes=30, store=FakeStore(events))
        self.assertGreater(out["bearish_score"], 0.7)
        self.assertEqual(out["event_count"], 3)
        self.assertGreater(out["confidence"], 0.0)
        self.assertLessEqual(out["bearish_score"], 1.0)
        self.assertLessEqual(len(out["top_events"]), 3)
        ids = {t.get("event_id") for t in out["top_events"]}
        self.assertTrue(ids)

    def test_old_events_outside_window_do_not_contribute(self):
        events = [
            _ev(event_id="old", minutes_ago=90, impact=-1.0, description="ancient crash"),
        ]
        out = market_pulse_score(since_minutes=30, store=FakeStore(events))
        self.assertEqual(out["bearish_score"], 0.0)
        self.assertEqual(out["event_count"], 0)

    def test_heavily_decayed_events_give_low_contribution(self):
        """Fake store ignores since_iso so decay is the only suppressor."""

        class NoFilterStore:
            def list_events(self, **_kw):
                return [
                    _ev(
                        event_id="decayed",
                        minutes_ago=120,
                        impact=-1.0,
                        description="old but returned",
                    )
                ]

        out = market_pulse_score(since_minutes=30, store=NoFilterStore())
        self.assertEqual(out["event_count"], 1)
        # decay = 0.5 ** (120/30) = 0.0625; avg ~ -0.0625; score = 0.15625
        self.assertLess(out["bearish_score"], 0.25)
        self.assertGreater(out["bearish_score"], 0.0)

    def test_store_exception_fails_open_to_neutral(self):
        out = market_pulse_score(since_minutes=30, store=FakeStore(boom=True))
        self.assertEqual(
            out,
            {
                "bearish_score": 0.0,
                "confidence": 0.0,
                "event_count": 0,
                "top_events": [],
            },
        )

    def test_list_events_called_with_symbol_none_not_embeddings(self):
        store = FakeStore([])
        market_pulse_score(since_minutes=30, store=store)
        self.assertTrue(store.calls)
        for c in store.calls:
            self.assertIsNone(c["symbol"])
            self.assertIn(c["event_type"], (
                "macro_news",
                "structure_risk",
                "onchain_tvl_shock",
                "token_unlock",
            ))


class TestGetCachedMarketPulse(unittest.TestCase):
    def setUp(self):
        import intelligence.memory.market_pulse as mp

        mp._CACHE["result"] = None
        mp._CACHE["computed_at"] = 0.0

    def tearDown(self):
        import intelligence.memory.market_pulse as mp

        mp._CACHE["result"] = None
        mp._CACHE["computed_at"] = 0.0

    def test_missing_cache_returns_neutral(self):
        out = get_cached_market_pulse(max_age_sec=60)
        self.assertEqual(out["bearish_score"], 0.0)
        self.assertEqual(out["confidence"], 0.0)
        self.assertEqual(out["event_count"], 0)
        self.assertEqual(out["top_events"], [])

    def test_fresh_cache_returned(self):
        import intelligence.memory.market_pulse as mp

        cached = {
            "bearish_score": 0.8,
            "confidence": 0.5,
            "event_count": 3,
            "top_events": [{"event_id": "n1"}],
        }
        mp._CACHE["result"] = cached
        mp._CACHE["computed_at"] = time.time()
        out = get_cached_market_pulse(max_age_sec=60)
        self.assertAlmostEqual(out["bearish_score"], 0.8)
        self.assertAlmostEqual(out["confidence"], 0.5)
        self.assertEqual(out["event_count"], 3)
        self.assertEqual(out["top_events"][0]["event_id"], "n1")

    def test_stale_cache_returns_neutral(self):
        import intelligence.memory.market_pulse as mp

        mp._CACHE["result"] = {
            "bearish_score": 0.9,
            "confidence": 1.0,
            "event_count": 9,
            "top_events": [],
        }
        mp._CACHE["computed_at"] = time.time() - 10_000
        out = get_cached_market_pulse(max_age_sec=30)
        self.assertEqual(out["bearish_score"], 0.0)
        self.assertEqual(out["confidence"], 0.0)
        self.assertEqual(out["event_count"], 0)


if __name__ == "__main__":
    unittest.main()
