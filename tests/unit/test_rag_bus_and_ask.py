"""Epic #72 C3 bus contracts + C4 ask-bridge RAG (no ledger writes)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from bus.schemas import RagQuery, RagResult
from services.telegram_ask_bridge import (
    _build_ask_rag_prompt,
    _extract_symbol_from_question,
    _grok_fallback_answer,
)


class TestRagBusContracts(unittest.TestCase):
    def test_rag_query_result_json_serializable(self):
        import json

        q = RagQuery(query="ARIA DCA", top_k=3, filters={"symbol": "ARIA/USDT"})
        r = RagResult(correlation_id=q.correlation_id, hits=[{"text": "t", "score": 0.5}])
        self.assertTrue(json.dumps(q.to_dict()))
        self.assertTrue(json.dumps(r.to_dict()))
        self.assertEqual(q.to_dict()["filters"]["symbol"], "ARIA/USDT")

    def test_publish_rag_request_noop_when_bus_off(self):
        from bus.publisher import publish_rag_request

        with patch("intelligence.memory.rag_config.rag_config", return_value={"use_bus": False}):
            ok = publish_rag_request(RagQuery(query="x"))
        self.assertFalse(ok)

    def test_publish_rag_request_force_without_redis_returns_false(self):
        from bus.publisher import publish_rag_request

        with patch("bus.publisher.get_redis", return_value=None):
            ok = publish_rag_request(RagQuery(query="x"), force=True)
        self.assertFalse(ok)


class TestAskBridgeRag(unittest.TestCase):
    def test_extract_symbol(self):
        self.assertEqual(_extract_symbol_from_question("DCA for ARIA/USDT bitte"), "ARIA/USDT")

    def test_build_prompt_includes_retrieved(self):
        from hermes.memory.rag_retriever import RagRetriever

        r = RagRetriever.in_memory(
            config={"memory": {"rag": {"enabled": True, "embedding_backend": "hash"}}}
        )
        r.add_to_memory(
            "Trade ARIA/USDT sell pnl=-80 reason=stop",
            {"type": "trade", "symbol": "ARIA/USDT", "source_id": "a1"},
        )
        with patch("intelligence.memory.rag_config.rag_enabled", return_value=True):
            prompt = _build_ask_rag_prompt(
                "DCA: last trades ARIA/USDT, nachkaufen?",
                {"symbol": "ARIA/USDT"},
                retriever=r,
            )
        self.assertIn("ARIA", prompt)
        self.assertIn("RETRIEVED_MEMORY", prompt)

    def test_aria_context_trades_not_blocked_by_unrelated_store(self):
        """Honest path: polluted store + ARIA only in context.recent_trades → ARIA in prompt."""
        from hermes.memory.rag_retriever import RagRetriever

        r = RagRetriever.in_memory(
            config={"memory": {"rag": {"enabled": True, "embedding_backend": "hash"}}}
        )
        # Pollution that would win naive unfiltered retrieve
        r.add_to_memory(
            "Trade BTC/USDT buy pnl=12 reason=breakout funding spike",
            {"type": "trade", "symbol": "BTC/USDT", "source_id": "btc1"},
        )
        r.add_to_memory(
            "Trade ETH/USDT sell pnl=-3 reason=rsi",
            {"type": "trade", "symbol": "ETH/USDT", "source_id": "eth1"},
        )
        ctx = {
            "symbol": "ARIA/USDT",
            "recent_trades": [
                {
                    "symbol": "ARIA/USDT",
                    "direction": "sell",
                    "pnl_usdt": -1768,
                    "reason": "stop after dca blowup unique_aria_marker",
                }
            ],
        }
        with patch("intelligence.memory.rag_config.rag_enabled", return_value=True):
            prompt = _build_ask_rag_prompt(
                "DCA: last trades for ARIA/USDT — should we add now and how much?",
                ctx,
                retriever=r,
            )
        # Must appear in RETRIEVED_MEMORY, not only AKTUELLER KONTEXT (which dumps recent_trades JSON)
        self.assertIn("RETRIEVED_MEMORY", prompt)
        after = prompt.split("RETRIEVED_MEMORY", 1)[1]
        self.assertIn("unique_aria_marker", after)
        self.assertIn("-1768", after)
        self.assertIn("ARIA/USDT", after)
        # Ephemeral seed must not persist into the store
        store_blob = " ".join(c.text for c in r._store.list_chunks(limit=50))
        self.assertNotIn("unique_aria_marker", store_blob)

    def test_grok_answer_does_not_call_order_apis(self):
        calls = []

        def _ask(prompt, temperature=0.3):
            calls.append(prompt)
            return "Für ARIA eher kein DCA — letzter Trade war Stop."

        with patch("services.telegram_ask_bridge._build_ask_rag_prompt", return_value="PROMPT"), patch(
            "grok_agent.ask_grok", side_effect=_ask
        ):
            ans = _grok_fallback_answer("DCA ARIA?", {"symbol": "ARIA/USDT"})
        self.assertIn("ARIA", ans)
        self.assertTrue(calls)
        # Static: ask path modules must not invoke execute helpers
        import inspect

        src = inspect.getsource(_grok_fallback_answer)
        for banned in ("create_market_buy", "execute_order", "record_trade", "init_position"):
            self.assertNotIn(banned, src)


class TestRagIndex(unittest.TestCase):
    def test_index_from_fake_memory_store(self):
        from hermes.memory.rag_retriever import RagRetriever
        from intelligence.memory.models import Lesson, TradeMemory
        from intelligence.memory.rag_index import index_store_into_rag

        class FakeMem:
            def list_lessons(self, limit=40):
                return [
                    Lesson(
                        lesson_id="les1",
                        text="ARIA weak after DCA cascade",
                        symbols=["ARIA/USDT"],
                    )
                ]

            def list_trades(self, limit=40):
                return [
                    TradeMemory(
                        trade_id="tr1",
                        symbol="ARIA/USDT",
                        direction="sell",
                        pnl_usdt=-50.0,
                        reason="stop",
                        outcome="loss",
                    )
                ]

            def list_events(self, limit=40):
                return []

        r = RagRetriever.in_memory(
            config={"memory": {"rag": {"enabled": True, "index_on_cycle": True}}}
        )
        out = index_store_into_rag(
            FakeMem(),
            r,
            config={"memory": {"rag": {"enabled": True, "index_on_cycle": True}}},
        )
        self.assertGreaterEqual(out["lessons"] + out["trades"], 1)
        hits = r.retrieve("ARIA DCA stop", top_k=3)
        self.assertTrue(hits)


if __name__ == "__main__":
    unittest.main()
