"""#103 apply facts into DcaContext via shipped helpers."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from intelligence.memory.coin_facts import apply_facts_to_context, flags_from_events
from intelligence.memory.models import MarketEvent
from strategies.dca_policy import DcaContext


class TestCoinFactContext(unittest.TestCase):
    def test_apply_facts_sets_hard_negative(self):
        ctx = DcaContext(symbol="ALLO/USDT")
        ev = MarketEvent(
            event_id="t1",
            timestamp="2026-07-18T12:00:00Z",
            event_type="hack",
            symbols=["ALLO/USDT"],
            impact_score=-0.9,
            description="bridge hack",
            source="cmc_ai_updates",
        )
        apply_facts_to_context(
            ctx,
            config_raw={"memory": {"coin_facts": {"enabled": True, "policy_apply": True}}},
            events=[ev],
        )
        self.assertTrue(ctx.fact_hard_negative)
        self.assertGreaterEqual(ctx.fact_event_count, 1)
        self.assertIn("hack", ctx.fact_summary)

    def test_disabled_does_not_load_store(self):
        ctx = DcaContext(symbol="ALLO/USDT")
        with patch(
            "intelligence.memory.coin_facts.summarize_facts_for_symbol"
        ) as sum_fn:
            apply_facts_to_context(
                ctx,
                config_raw={"memory": {"coin_facts": {"enabled": False}}},
            )
            sum_fn.assert_not_called()
        self.assertFalse(ctx.fact_hard_negative)

    def test_build_dca_context_wires_facts_when_enabled(self):
        from strategies.dca_context import build_dca_context

        ev = MarketEvent(
            event_id="t2",
            timestamp="2026-07-18T12:00:00Z",
            event_type="profit_taking_narrative",
            symbols=["ALLO/USDT"],
            impact_score=-0.4,
            description="profit taking cools",
            source="cmc_ai_updates",
        )

        class FakeStore:
            def list_events(self, **kwargs):
                return [ev]

        raw = {
            "memory": {
                "coin_facts": {
                    "enabled": True,
                    "policy_apply": True,
                    "lookback_hours": 72,
                }
            },
            "regime_detector": {"enabled": False},
        }
        with patch(
            "intelligence.memory.coin_facts.MemoryStore",
            FakeStore,
            create=True,
        ), patch(
            "intelligence.memory.store.MemoryStore",
            FakeStore,
        ), patch(
            "services.market_policy_fusion.get_global_market_bias",
            return_value={"size_mult": 1.0, "block_buys": False},
        ):
            # apply_facts uses MemoryStore() inside summarize — patch at store module
            with patch(
                "intelligence.memory.coin_facts.summarize_facts_for_symbol",
                return_value=flags_from_events([ev]),
            ):
                ctx = build_dca_context(
                    symbol="ALLO/USDT",
                    include_rag=False,
                    config_raw=raw,
                )
        self.assertTrue(ctx.fact_profit_taking)
        self.assertGreaterEqual(ctx.fact_event_count, 1)


if __name__ == "__main__":
    unittest.main()
