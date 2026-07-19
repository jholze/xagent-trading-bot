"""Large move → trigger attribution (memory only)."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from intelligence.memory.models import MarketEvent, utc_now_iso
from intelligence.memory.move_attribution import (
    MoveSnap,
    build_attribution_event,
    find_triggers,
    is_large_move,
    score_trigger_for_move,
    sync_move_attribution,
)


class FakeStore:
    def __init__(self):
        self.events: dict[str, MarketEvent] = {}

    def get_event(self, eid: str):
        return self.events.get(eid)

    def upsert_event(self, ev: MarketEvent) -> bool:
        self.events[ev.event_id] = ev
        return True

    def list_events(self, symbol=None, event_type=None, since_iso=None, limit=50):
        out = list(self.events.values())
        if symbol:
            base = symbol.split("/")[0]
            out = [
                e
                for e in out
                if symbol in (e.symbols or [])
                or any(base in s for s in (e.symbols or []))
            ]
        if event_type:
            out = [e for e in out if e.event_type == event_type]
        if since_iso:
            out = [e for e in out if (e.timestamp or "") >= since_iso]
        return out[:limit]


class TestLargeMove(unittest.TestCase):
    def test_abs_threshold(self):
        self.assertTrue(is_large_move(MoveSnap("ETH/USDT", 15.0)))
        self.assertFalse(is_large_move(MoveSnap("ETH/USDT", 5.0)))

    def test_vs_btc_idiosyncratic(self):
        # Only +6% absolute but +10 vs BTC
        self.assertTrue(
            is_large_move(
                MoveSnap("ALT/USDT", 6.0, vs_btc=10.0),
                abs_chg=12.0,
                rel_btc=8.0,
            )
        )


class TestTriggerScore(unittest.TestCase):
    def test_unlock_near_dump_scores_high(self):
        now = datetime.now(timezone.utc)
        move = MoveSnap("ARB/USDT", -18.0)
        unlock = MarketEvent(
            event_id="u1",
            timestamp=(now - timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            event_type="token_unlock",
            symbols=["ARB/USDT"],
            impact_score=-0.4,
            description="Major ARB unlock vesting",
            source="coin_facts",
        )
        sc = score_trigger_for_move(move=move, event=unlock, now=now)
        self.assertGreater(sc, 0.4)

    def test_unrelated_symbol_zero(self):
        move = MoveSnap("ETH/USDT", 20.0)
        ev = MarketEvent(
            event_id="x",
            timestamp=utc_now_iso(),
            event_type="token_unlock",
            symbols=["DOGE/USDT"],
            description="doge unlock",
            source="x",
        )
        self.assertEqual(score_trigger_for_move(move=move, event=ev), 0.0)


class TestFindAndWrite(unittest.TestCase):
    def test_sync_writes_attribution_with_triggers(self):
        store = FakeStore()
        now = datetime.now(timezone.utc)
        store.upsert_event(
            MarketEvent(
                event_id="soc1",
                timestamp=(now - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                event_type="lc_social_spike",
                symbols=["SOL/USDT"],
                impact_score=0.4,
                description="SOL social spike",
                source="lunarcrush",
            )
        )
        moves = [MoveSnap("SOL/USDT", 22.0, source="test")]
        out = sync_move_attribution(
            store,
            config_raw={
                "memory": {
                    "enabled": True,
                    "move_attribution": {
                        "enabled": True,
                        "abs_chg_24h_pct": 12,
                        "index_rag": False,
                    },
                }
            },
            symbols=["SOL/USDT"],
            moves=moves,
        )
        self.assertTrue(out.get("enabled"))
        self.assertEqual(out.get("moves_large"), 1)
        self.assertGreaterEqual(out.get("attributions_written"), 1)
        self.assertGreaterEqual(out.get("links_found"), 1)
        written = [e for e in store.events.values() if e.event_type == "price_move_attribution"]
        self.assertEqual(len(written), 1)
        self.assertIn("triggers", written[0].metadata)
        self.assertEqual(written[0].metadata["triggers"][0]["event_id"], "soc1")

    def test_build_event_no_triggers(self):
        ev = build_attribution_event(MoveSnap("X/USDT", -14.0), [])
        self.assertEqual(ev.event_type, "price_move_attribution")
        self.assertIn("no strong trigger", ev.description.lower())


if __name__ == "__main__":
    unittest.main()
