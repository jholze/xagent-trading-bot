"""Large move → 1h screen + 15m drill + trigger attribution."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pandas as pd

from intelligence.memory.models import MarketEvent, utc_now_iso
from intelligence.memory.move_attribution import (
    MoveSnap,
    apply_15m_drill,
    build_attribution_event,
    drill_15m,
    find_triggers,
    is_large_move,
    pct_change_from_closes,
    score_trigger_for_move,
    strongest_bar_impulse,
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


class TestCandleMath(unittest.TestCase):
    def test_pct_change_1_bar(self):
        # 100 → 105 = +5%
        self.assertAlmostEqual(pct_change_from_closes([100.0, 105.0], 1), 5.0)

    def test_strongest_impulse_picks_big_bar(self):
        closes = [100, 101, 102, 110, 109]  # +7.8% bar
        vols = [1, 1, 1, 5, 1]
        imp, vol_x = strongest_bar_impulse(closes, vols, window=4)
        self.assertIsNotNone(imp)
        self.assertGreater(abs(imp), 5)
        self.assertIsNotNone(vol_x)
        self.assertGreater(vol_x, 1.5)


class TestLargeMove(unittest.TestCase):
    def test_1h_threshold(self):
        self.assertTrue(is_large_move(MoveSnap("ETH/USDT", chg_1h=5.0, chg_24h=5.0), abs_chg=4.0))
        self.assertFalse(is_large_move(MoveSnap("ETH/USDT", chg_1h=2.0, chg_24h=2.0), abs_chg=4.0))

    def test_vs_btc(self):
        self.assertTrue(
            is_large_move(
                MoveSnap("ALT/USDT", chg_1h=2.0, chg_24h=2.0, vs_btc=4.0),
                abs_chg=4.0,
                rel_btc=3.0,
            )
        )


class TestTriggerScore(unittest.TestCase):
    def test_unlock_near_dump_scores_high(self):
        now = datetime.now(timezone.utc)
        move = MoveSnap("ARB/USDT", chg_1h=-6.0, chg_24h=-6.0, screen_tf="1h")
        unlock = MarketEvent(
            event_id="u1",
            timestamp=(now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            event_type="token_unlock",
            symbols=["ARB/USDT"],
            impact_score=-0.4,
            description="Major ARB unlock vesting",
            source="coin_facts",
        )
        sc = score_trigger_for_move(move=move, event=unlock, now=now)
        self.assertGreater(sc, 0.4)

    def test_prefers_news_before_move_over_after(self):
        """Catalyst *before* the move must rank higher than reactive headline after."""
        now = datetime.now(timezone.utc)
        move = MoveSnap(
            "SOL/USDT",
            chg_1h=8.0,
            chg_24h=8.0,
            screen_tf="1h",
            chg_1h_bars=1,
            move_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        before = MarketEvent(
            event_id="before",
            timestamp=(now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            event_type="listing",
            symbols=["SOL/USDT"],
            impact_score=0.3,
            description="SOL listing announcement major exchange",
            source="news",
        )
        after = MarketEvent(
            event_id="after",
            timestamp=(now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            event_type="news",
            symbols=["SOL/USDT"],
            impact_score=0.2,
            description="SOL pumps 8% as traders pile in",
            source="news",
        )
        sc_b = score_trigger_for_move(
            move=move, event=before, now=now, prefer_pre_move=True
        )
        sc_a = score_trigger_for_move(
            move=move, event=after, now=now, prefer_pre_move=True
        )
        self.assertGreater(sc_b, sc_a)
        self.assertGreater(sc_b, 0.3)


class Test15mDrill(unittest.TestCase):
    def test_drill_finds_impulse(self):
        # synthetic 15m: flat then spike
        closes = [10 + i * 0.01 for i in range(20)] + [10.2, 10.9, 10.85]
        vols = [1.0] * (len(closes) - 2) + [8.0, 2.0]
        self.assertEqual(len(closes), len(vols))
        df = pd.DataFrame({"close": closes, "volume": vols})
        market = MagicMock()
        market.fetch_ohlcv.return_value = df
        detail = drill_15m("SOL/USDT", market=market, fine_bars=8, impulse_min_pct=1.0)
        self.assertEqual(detail["fine_tf"], "15m")
        self.assertIsNotNone(detail["fine_impulse_pct"])
        self.assertGreater(abs(detail["fine_impulse_pct"]), 1.0)

    def test_apply_15m_on_large_snap(self):
        closes = [100.0] * 10 + [100.0, 106.0, 105.5]
        df = pd.DataFrame({"close": closes, "volume": [1.0] * len(closes)})
        market = MagicMock()
        market.fetch_ohlcv.return_value = df
        snap = MoveSnap("SOL/USDT", chg_1h=5.0, chg_24h=5.0, screen_tf="1h", source="ohlcv_1h")
        apply_15m_drill(
            snap,
            cfg={"fine_bars": 6, "ohlcv_limit_15m": 30, "fine_impulse_min_pct": 1.0},
            market=market,
        )
        self.assertEqual(snap.fine_tf, "15m")
        self.assertIsNotNone(snap.fine_impulse_pct)


class TestSyncWithDrill(unittest.TestCase):
    def test_sync_writes_with_1h_and_15m_meta(self):
        store = FakeStore()
        now = datetime.now(timezone.utc)
        store.upsert_event(
            MarketEvent(
                event_id="soc1",
                timestamp=(now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                event_type="lc_social_spike",
                symbols=["SOL/USDT"],
                impact_score=0.4,
                description="SOL social spike",
                source="lunarcrush",
            )
        )
        moves = [
            MoveSnap(
                "SOL/USDT",
                chg_1h=5.5,
                chg_24h=5.5,
                screen_tf="1h",
                source="ohlcv_1h",
                fine_tf="15m",
                fine_impulse_pct=3.2,
                fine_impulse_vol_x=2.5,
                fine_bars_scanned=8,
            )
        ]
        out = sync_move_attribution(
            store,
            config_raw={
                "memory": {
                    "enabled": True,
                    "move_attribution": {
                        "enabled": True,
                        "abs_chg_1h_pct": 4,
                        "index_rag": False,
                    },
                }
            },
            symbols=["SOL/USDT"],
            moves=moves,
        )
        self.assertEqual(out.get("moves_large"), 1)
        self.assertGreaterEqual(out.get("attributions_written"), 1)
        written = [e for e in store.events.values() if e.event_type == "price_move_attribution"]
        self.assertEqual(len(written), 1)
        self.assertIn("1h", written[0].description)
        self.assertIn("15m_impulse", written[0].description)
        self.assertEqual(written[0].metadata.get("screen_tf"), "1h")
        self.assertEqual(written[0].metadata.get("fine_impulse_pct"), 3.2)


class TestBuildEvent(unittest.TestCase):
    def test_no_triggers(self):
        ev = build_attribution_event(
            MoveSnap("X/USDT", chg_1h=-5.0, chg_24h=-5.0, screen_tf="1h"), []
        )
        self.assertIn("preceding", ev.description.lower())

    def test_find_triggers_marks_before(self):
        store = FakeStore()
        now = datetime.now(timezone.utc)
        store.upsert_event(
            MarketEvent(
                event_id="pre1",
                timestamp=(now - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                event_type="token_unlock",
                symbols=["WIF/USDT"],
                description="WIF unlock",
                source="news",
                impact_score=-0.2,
            )
        )
        move = MoveSnap(
            "WIF/USDT",
            chg_1h=-5.0,
            chg_24h=-5.0,
            screen_tf="1h",
            move_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        hits = find_triggers(store, move, now=now, prefer_pre_move=True)
        self.assertTrue(hits)
        self.assertEqual(hits[0].relation, "before")
        self.assertIsNotNone(hits[0].hours_delta)
        self.assertLessEqual(hits[0].hours_delta, 0)


if __name__ == "__main__":
    unittest.main()
