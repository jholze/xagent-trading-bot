"""#100 DCA policy reflection — pure rules + store write."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from intelligence.memory.dca_reflector import (
    derive_dca_lesson_specs,
    reflect_dca_policy,
)
from intelligence.memory.models import TradeMemory
from intelligence.memory.store import InMemoryMemoryStore


def _ev(
    *,
    symbol: str,
    skip: bool,
    reasons: list[str],
    mode: str = "HARVEST",
    ts: str = "2026-07-18T12:00:00Z",
    eid: str = "e1",
):
    return SimpleNamespace(
        event_id=eid,
        event_type="dca_decision",
        timestamp=ts,
        symbols=[symbol],
        metadata={
            "kind": "dca_decision",
            "skip": skip,
            "reason_codes": reasons,
            "cash_mode": mode,
            "size_mult": 1.0,
        },
        description="test",
    )


class TestDeriveDcaLessons(unittest.TestCase):
    def test_harvest_skip_global_lesson(self):
        events = [
            _ev(symbol="ADA/USDT", skip=True, reasons=["harvest_skip"], eid=f"h{i}")
            for i in range(6)
        ]
        specs = derive_dca_lesson_specs(events, [], min_events=5)
        keys = [s["key"] for s in specs]
        self.assertIn("global|harvest_skip", keys)
        harvest = next(s for s in specs if s["key"] == "global|harvest_skip")
        self.assertIn("harvest_skip", harvest["text"])
        self.assertGreaterEqual(harvest["sample_n"], 5)

    def test_allow_then_loss_outcome(self):
        events = [
            _ev(
                symbol="ZBT/USDT",
                skip=False,
                reasons=["deploy_boost"],
                mode="DEPLOY",
                ts="2026-07-18T10:00:00Z",
                eid=f"a{i}",
            )
            for i in range(3)
        ]
        sells = [
            TradeMemory(
                trade_id=f"s{i}",
                symbol="ZBT/USDT",
                direction="sell",
                exit_time="2026-07-18T12:00:00Z",
                entry_time="2026-07-18T12:00:00Z",
                pnl_usdt=-80.0,
                outcome="loss",
            )
            for i in range(3)
        ]
        specs = derive_dca_lesson_specs(
            events,
            sells,
            min_events=5,
            outcome_window_hours=72,
            loss_usdt_threshold=50,
        )
        # need >= 2 loss hits for allow_then_loss
        keys = [s["key"] for s in specs]
        self.assertTrue(any("allow_then_loss" in k for k in keys))

    def test_too_few_events_no_harvest_lesson(self):
        events = [
            _ev(symbol="ADA/USDT", skip=True, reasons=["harvest_skip"], eid="only1"),
            _ev(symbol="ADA/USDT", skip=True, reasons=["harvest_skip"], eid="only2"),
        ]
        specs = derive_dca_lesson_specs(events, [], min_events=5)
        self.assertFalse(any(s["key"] == "global|harvest_skip" for s in specs))


class TestReflectDcaPolicyStore(unittest.TestCase):
    def test_writes_lessons_to_store(self):
        store = InMemoryMemoryStore()
        from intelligence.memory.models import MarketEvent

        for i in range(6):
            store.upsert_event(
                MarketEvent(
                    event_id=f"dca_e{i}",
                    timestamp="2026-07-18T12:00:00Z",
                    event_type="dca_decision",
                    symbols=["ADA/USDT"],
                    description=f"DCA decision ADA harvest {i}",
                    source="dca_policy",
                    metadata={
                        "kind": "dca_decision",
                        "skip": True,
                        "reason_codes": ["harvest_skip"],
                        "cash_mode": "HARVEST",
                    },
                )
            )
        out = reflect_dca_policy(
            store,
            config_raw={
                "memory": {
                    "dca_reflect": {
                        "enabled": True,
                        "min_events": 5,
                        "reflect_grok": False,
                        "index_rag": False,
                    }
                }
            },
        )
        self.assertGreaterEqual(out.get("lessons", 0), 1)
        self.assertGreaterEqual(out.get("events_read", 0), 6)
        lessons = store.list_lessons(limit=20)
        self.assertTrue(any("harvest" in (l.text or "").lower() for l in lessons))


if __name__ == "__main__":
    unittest.main()
