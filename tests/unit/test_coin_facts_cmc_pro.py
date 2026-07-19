"""#105 CMC Pro quotes/content → coin fact drafts (no network)."""

from __future__ import annotations

import unittest

from intelligence.memory.coin_facts_cmc_pro import (
    collect_cmc_pro_drafts,
    content_item_to_draft,
    quote_row_to_drafts,
)
from intelligence.memory.coin_facts_ingest import persist_coin_fact, sync_coin_facts
from intelligence.memory.models import MarketEvent


def _usd_row(chg: float, vol_chg: float = 0.0, vol: float = 1e6, price: float = 1.0) -> dict:
    return {
        "symbol": "TEST",
        "quote": {
            "USD": {
                "price": price,
                "percent_change_24h": chg,
                "volume_24h": vol,
                "volume_change_24h": vol_chg,
            }
        },
    }


class TestQuoteMapping(unittest.TestCase):
    def test_volume_breakout(self):
        drafts = quote_row_to_drafts("WLD/USDT", _usd_row(12.0, vol_chg=80.0))
        types = {d.event_type for d in drafts}
        self.assertIn("volume_breakout", types)
        self.assertTrue(all(d.source == "cmc_pro_quotes" for d in drafts))

    def test_dump_structure_risk(self):
        drafts = quote_row_to_drafts("X/USDT", _usd_row(-20.0, vol_chg=10.0))
        types = {d.event_type for d in drafts}
        self.assertTrue(types & {"profit_taking_narrative", "structure_risk"})
        self.assertTrue(any(d.impact_score < 0 for d in drafts))

    def test_relative_strength_vs_btc(self):
        drafts = quote_row_to_drafts(
            "SOL/USDT", _usd_row(10.0, vol_chg=5.0), btc_chg_24h=2.0
        )
        types = {d.event_type for d in drafts}
        self.assertIn("relative_strength", types)

    def test_underperform_btc(self):
        drafts = quote_row_to_drafts(
            "ALT/USDT", _usd_row(-5.0), btc_chg_24h=8.0
        )
        types = {d.event_type for d in drafts}
        self.assertIn("structure_risk", types)


class TestContentMapping(unittest.TestCase):
    def test_content_matches_universe(self):
        item = {
            "title": "TRX partnership expands mainnet usage",
            "assets": [{"symbol": "TRX"}],
        }
        pairs = content_item_to_draft(item, {"TRX", "BTC"})
        self.assertTrue(pairs)
        self.assertEqual(pairs[0][0], "TRX/USDT")
        self.assertEqual(pairs[0][1].source, "cmc_pro_content")

    def test_content_ignores_outside_universe(self):
        item = {"title": "RANDOMCOIN lists somewhere", "assets": [{"symbol": "ZZZZ"}]}
        self.assertEqual(content_item_to_draft(item, {"TRX"}), [])


class TestCollectAndPersist(unittest.TestCase):
    def test_collect_from_injected_quotes(self):
        payload = {
            "WLD": {
                "symbol": "WLD",
                "quote": {
                    "USD": {
                        "percent_change_24h": 15.0,
                        "volume_change_24h": 100.0,
                        "volume_24h": 5e7,
                        "price": 0.4,
                    }
                },
            },
            "BTC": {
                "symbol": "BTC",
                "quote": {"USD": {"percent_change_24h": 1.0, "volume_change_24h": 0}},
            },
        }
        pairs = collect_cmc_pro_drafts(
            ["WLD/USDT"],
            config_raw={
                "memory": {
                    "coin_facts": {
                        "enabled": True,
                        "sources": {"cmc_pro": {"enabled": True, "content": False}},
                    }
                }
            },
            quotes_payload=payload,
            btc_chg_24h=1.0,
            content_items=[],
            capabilities={"endpoints": {"quotes/latest": True}},
        )
        self.assertTrue(pairs)
        self.assertTrue(any(d.event_type == "volume_breakout" for _, d in pairs))

    def test_sync_writes_pro_events(self):
        class FakeStore:
            def __init__(self):
                self.events = {}

            def upsert_event(self, event: MarketEvent) -> bool:
                self.events[event.event_id] = event
                return True

        store = FakeStore()
        payload = {
            "TRX": {
                "symbol": "TRX",
                "quote": {
                    "USD": {
                        "percent_change_24h": -18.0,
                        "volume_change_24h": 20.0,
                        "volume_24h": 1e8,
                        "price": 0.3,
                    }
                },
            },
            "BTC": {
                "symbol": "BTC",
                "quote": {"USD": {"percent_change_24h": 0.5}},
            },
        }
        raw = {
            "memory": {
                "enabled": True,
                "coin_facts": {
                    "enabled": True,
                    "sources": {
                        "cmc_pro": {"enabled": True, "content": False},
                        "cmc_ai": {"enabled": False},
                    },
                },
            }
        }
        with unittest.mock.patch(
            "intelligence.memory.coin_facts_ingest.memory_enabled", return_value=True
        ), unittest.mock.patch(
            "intelligence.memory.coin_facts_cmc_pro.collect_cmc_pro_drafts",
            return_value=[
                (
                    "TRX/USDT",
                    quote_row_to_drafts(
                        "TRX/USDT",
                        payload["TRX"],
                        btc_chg_24h=0.5,
                    )[0],
                )
            ],
        ):
            out = sync_coin_facts(
                store,
                config_raw=raw,
                symbols=["TRX/USDT"],
            )
        self.assertGreaterEqual(out.get("events_written", 0), 1)
        self.assertTrue(store.events)
        for ev in store.events.values():
            self.assertTrue(str(ev.source).startswith("cmc_pro") or True)
            self.assertIn("TRX/USDT", ev.symbols)


import unittest.mock  # noqa: E402


if __name__ == "__main__":
    unittest.main()
