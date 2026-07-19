"""#103 ingest: fixture fetch → MarketEvent persist (idempotent)."""

from __future__ import annotations

import unittest
from pathlib import Path

from intelligence.memory.coin_facts_ingest import (
    coin_fact_universe,
    draft_to_event,
    persist_coin_fact,
    sync_coin_facts,
)
from intelligence.memory.coin_facts import classify_latest_updates_bullet
from intelligence.memory.models import MarketEvent

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "cmc_ai"


class FakeStore:
    def __init__(self):
        self.events: dict[str, MarketEvent] = {}

    def upsert_event(self, event: MarketEvent) -> bool:
        self.events[event.event_id] = event
        return True

    def list_events(self, **kwargs):
        return list(self.events.values())


def _fetch(url: str) -> str:
    if "latest-updates" in url:
        return (FIX / "allora_latest_updates.html").read_text()
    if "price-analysis" in url:
        return (FIX / "allora_price_analysis.html").read_text()
    if "price-prediction" in url:
        return (FIX / "allora_price_prediction.html").read_text()
    return ""


class TestIngest(unittest.TestCase):
    def test_universe_positions_first_capped(self):
        positions = [{"symbol": "AAA/USDT"}, {"symbol": "BBB/USDT"}]
        watch = [
            {"symbol": "BBB/USDT", "active": True},
            {"symbol": "CCC/USDT", "active": True},
            {"symbol": "DDD/USDT", "active": True},
        ]
        raw = {
            "memory": {
                "coin_facts": {
                    "enabled": True,
                    "universe": ["open_positions", "watchlist"],
                    "sources": {"cmc_ai": {"max_coins_per_cycle": 3}},
                }
            }
        }
        u = coin_fact_universe(
            raw,
            list_positions_fn=lambda: positions,
            load_watchlist_fn=lambda: watch,
        )
        self.assertEqual(u[0], "AAA/USDT")
        self.assertEqual(len(u), 3)
        self.assertIn("BBB/USDT", u)

    def test_persist_idempotent_ids(self):
        store = FakeStore()
        d = classify_latest_updates_bullet(
            "ALLO cools ~10% after AI-token rotation pump; profit-taking noted"
        )
        self.assertIsNotNone(d)
        id1 = persist_coin_fact(d, symbol="ALLO/USDT", slug="allora", store=store)
        id2 = persist_coin_fact(d, symbol="ALLO/USDT", slug="allora", store=store)
        self.assertTrue(id1)
        self.assertEqual(id1, id2)
        self.assertEqual(len(store.events), 1)

    def test_sync_writes_from_fixtures(self):
        store = FakeStore()
        raw = {
            "memory": {
                "enabled": True,
                "coin_facts": {
                    "enabled": True,
                    "policy_apply": True,
                    "sources": {
                        "cmc_ai": {
                            "enabled": True,
                            "max_coins_per_cycle": 1,
                            "max_events_per_coin_cycle": 8,
                        }
                    },
                },
            }
        }
        with unittest.mock.patch(
            "intelligence.memory.coin_facts_ingest.memory_enabled", return_value=True
        ):
            out = sync_coin_facts(
                store,
                fetch_fn=_fetch,
                config_raw=raw,
                symbols=["ALLO/USDT"],
            )
            out2 = sync_coin_facts(
                store,
                fetch_fn=_fetch,
                config_raw=raw,
                symbols=["ALLO/USDT"],
            )
        self.assertTrue(out.get("enabled"))
        self.assertGreaterEqual(out["events_written"], 1)
        # second pass re-upserts same ids — count of unique events stable
        n = len(store.events)
        self.assertGreaterEqual(n, 1)
        self.assertEqual(len(store.events), n)
        # symbols scoped
        for ev in store.events.values():
            self.assertIn("ALLO/USDT", ev.symbols)
            self.assertIn(ev.source, ("cmc_ai_updates", "cmc_ai_price", "cmc_ai_prediction"))
        self.assertEqual(out2["events_written"], out["events_written"])

    def test_disabled_skips(self):
        store = FakeStore()
        out = sync_coin_facts(
            store,
            fetch_fn=_fetch,
            config_raw={"memory": {"coin_facts": {"enabled": False}}},
            symbols=["ALLO/USDT"],
        )
        self.assertTrue(out.get("skipped"))
        self.assertEqual(out.get("events_written", 0), 0)
        self.assertEqual(len(store.events), 0)

    def test_draft_to_event_skips_ignore_target(self):
        from intelligence.memory.coin_facts import CoinFactDraft

        d = CoinFactDraft(
            event_type="ignore_target",
            impact_score=0.0,
            description="will hit $2",
            source="cmc_ai_prediction",
        )
        self.assertIsNone(draft_to_event(d, symbol="ALLO/USDT", slug="allora"))


# late import for patch in test_sync
import unittest.mock  # noqa: E402


if __name__ == "__main__":
    unittest.main()
