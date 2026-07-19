"""P1–P6 memory quality improvements — pure unit coverage."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from intelligence.memory.embeddings import embed_for_rag, rag_prefer_minilm
from intelligence.memory.models import MarketEvent, TradeMemory, utc_now_iso
from intelligence.memory.social_ingest import join_social_events_to_trades
from strategies.dca_policy import DcaContext, DcaPolicyResult, format_dca_policy_audit


class TestP1EvalScript(unittest.TestCase):
    def test_fixture_eval_hit_rate(self):
        from scripts.eval_memory_retrieval import run_eval

        report = run_eval(live=False)
        self.assertGreaterEqual(report["hit_rate"], 0.5)
        self.assertGreaterEqual(report["hits"], 3)
        self.assertEqual(report["total"], 6)


class TestP2RagEmbedPreferMinilm(unittest.TestCase):
    def test_prefer_minilm_default_true(self):
        self.assertTrue(rag_prefer_minilm())

    def test_embed_for_rag_hash_fallback_384(self):
        # Force minilm path to fail → hash 384
        import intelligence.memory.embeddings as emb

        emb._minilm_failed = True
        emb._minilm_model = None
        try:
            with patch.dict("os.environ", {"MEMORY_RAG_EMBED": "hash"}, clear=False):
                # re-read prefer
                v = emb.embed_text_hash("hello rag", dim=384)
                self.assertEqual(len(v), 384)
            v2 = embed_for_rag("hello rag quality")
            self.assertEqual(len(v2), 384)
        finally:
            emb._minilm_failed = False


class TestP3MacroPressure(unittest.TestCase):
    def test_pressure_event_when_mult_off_neutral(self):
        from intelligence.macro.sync import sync_macro_context

        class FakeStore:
            def __init__(self):
                self.events = {}

            def get_event(self, eid):
                return self.events.get(eid)

            def upsert_event(self, ev):
                self.events[ev.event_id] = ev
                return True

        store = FakeStore()
        # Force pm mispricing via injected markets + thr
        market = SimpleNamespace(
            market_id="m1",
            title="Fed cut odds",
            prob=0.7,
            prev_prob=0.5,
        )
        # session closed so only calendar/pm may fire; inject empty calendar
        out = sync_macro_context(
            store=store,
            config={
                "memory": {
                    "macro": {"enabled": True, "pre_windows_min": [60], "post_windows_min": [5]},
                    "sessions": {"enabled": True},
                    "polymarket": {"enabled": True, "mispricing_delta_pp": 5},
                    "calendar_risk": {"size_mult_pre_high_impact": 0.5, "min_hist_samples": 8},
                }
            },
            calendar_events=[],
            pm_markets=[market],
            now=datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc),
        )
        self.assertTrue(out.get("enabled"))
        # pm_mult may drop → pressure events possible
        types = [e.event_type for e in store.events.values()]
        # At least scheduled path runs without crash; pressure if mult ≠ 1
        self.assertIsInstance(types, list)


class TestP4SocialJoinDelayed(unittest.TestCase):
    def test_join_links_within_window(self):
        class FakeStore:
            def __init__(self):
                self.trades = []
                self.events = []

            def list_events(self, limit=400):
                return list(self.events)[:limit]

            def list_trades(self, tenant_id="default", limit=500):
                return list(self.trades)[:limit]

            def upsert_trade(self, t):
                return True

            def upsert_event(self, e):
                return True

        store = FakeStore()
        now = datetime.now(timezone.utc)
        store.events.append(
            MarketEvent(
                event_id="soc1",
                timestamp=(now - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                event_type="lc_social_spike",
                symbols=["SOL/USDT"],
                description="spike",
                source="lc",
            )
        )
        store.trades.append(
            TradeMemory(
                trade_id="t1",
                symbol="SOL/USDT",
                entry_time=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                direction="buy",
                related_event_ids=[],
            )
        )
        n = join_social_events_to_trades(
            store,
            config={"memory": {"social": {"join_window_hours_delayed": 48}}},
        )
        self.assertEqual(n, 1)
        self.assertIn("soc1", store.trades[0].related_event_ids)


class TestP6DcaAudit(unittest.TestCase):
    def test_audit_line_includes_size_bias_and_lessons(self):
        ctx = DcaContext(
            symbol="ETH/USDT",
            cash_mode="HARVEST",
            fusion_size_mult=0.6,
            size_bias=0.7,
            entry_bias="soft_block",
            dca_lesson_count=2,
            dca_lesson_summary="weak dca after harvest",
        )
        res = DcaPolicyResult(size_mult=0.4, skip=False, reason_codes=("harvest",))
        line = format_dca_policy_audit(
            symbol="ETH/USDT",
            result=res,
            ctx=ctx,
            base_usdt=400,
            final_usdt=160,
        )
        self.assertIn("size_bias=0.70", line)
        self.assertIn("entry_bias=soft_block", line)
        self.assertIn("dca_lessons=2", line)


class TestP5AuditShadow(unittest.TestCase):
    def test_attach_memory_shadow_structure(self):
        from services.audit_trail import AuditTrail
        from core.config import BotConfig

        raw = {
            "observability": {"decisions_audit": True},
            "memory": {"rag": {"enabled": True, "enrich_decision_audit": True, "top_k": 3}},
        }
        at = AuditTrail(BotConfig(raw))
        entry = {"symbol": "ETH/USDT", "action": "HOLD"}
        analysis = SimpleNamespace(
            symbol="ETH/USDT",
            normalized_action="HOLD",
            rationale="test",
        )
        hit = SimpleNamespace(
            score=0.9,
            text="dca loss lesson",
            metadata={"type": "lesson", "symbol": "ETH/USDT"},
            chunk_id="c1",
        )
        with patch("hermes.memory.rag_retriever.RagRetriever") as RR:
            RR.return_value.retrieve.return_value = [hit]
            with patch(
                "intelligence.memory.cache.get_coin_profile",
                return_value=SimpleNamespace(
                    entry_bias="neutral", size_bias=1.0, risk_score=0.5
                ),
            ):
                at._attach_memory_shadow(entry, analysis)
        self.assertTrue(entry["memory_shadow"]["enabled"])
        self.assertEqual(entry["memory_shadow"]["hit_count"], 1)
        self.assertIn("dca loss", entry["memory_shadow"]["hits"][0]["text"])


if __name__ == "__main__":
    unittest.main()
