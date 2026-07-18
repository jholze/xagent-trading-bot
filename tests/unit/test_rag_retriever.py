"""Epic #72: RagRetriever add/retrieve/prompt + fail-open (no GPU, no ledger)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from hermes.memory.rag_retriever import RagRetriever
from intelligence.memory.rag_store import InMemoryRagBackend, RagStore, filter_matches, rank_chunks


class TestRagRetriever(unittest.TestCase):
    def setUp(self):
        self.cfg = {"memory": {"rag": {"enabled": True, "embedding_backend": "hash", "top_k": 5}}}
        self.retriever = RagRetriever.in_memory(config=self.cfg)

    def test_add_and_retrieve_ranks_relevant(self):
        self.retriever.add_to_memory(
            "Trade ARIA/USDT sell pnl_usdt=-120 reason=stop loss after DCA",
            {"type": "trade", "symbol": "ARIA/USDT", "source_id": "t1"},
        )
        self.retriever.add_to_memory(
            "Trade BTC/USDT buy pnl_usdt=40 reason=breakout",
            {"type": "trade", "symbol": "BTC/USDT", "source_id": "t2"},
        )
        self.retriever.add_to_memory(
            "ARIA weak history avoid chasing DCA after large stop",
            {"type": "lesson", "symbol": "ARIA/USDT", "source_id": "l1"},
        )
        hits = self.retriever.retrieve("ARIA DCA after stop loss", top_k=3)
        self.assertTrue(hits)
        blob = " ".join(h.text for h in hits).lower()
        self.assertIn("aria", blob)

    def test_filter_by_symbol(self):
        self.retriever.add_to_memory(
            "Trade ARIA/USDT sell pnl=-50",
            {"type": "trade", "symbol": "ARIA/USDT", "source_id": "a"},
        )
        self.retriever.add_to_memory(
            "Trade ETH/USDT sell pnl=-10",
            {"type": "trade", "symbol": "ETH/USDT", "source_id": "e"},
        )
        hits = self.retriever.retrieve("sell trade", top_k=5, filters={"symbol": "ETH/USDT"})
        self.assertTrue(hits)
        for h in hits:
            self.assertEqual(h.metadata.get("symbol"), "ETH/USDT")

    def test_filter_type_in(self):
        self.assertTrue(
            filter_matches({"type": "lesson"}, {"type": {"$in": ["lesson", "trade"]}})
        )
        self.assertFalse(filter_matches({"type": "event"}, {"type": {"$in": ["lesson"]}}))

    def test_build_prompt_contains_retrieved_memory(self):
        self.retriever.add_to_memory(
            "Trade SOL/USDT dca buy after dip",
            {"type": "trade", "symbol": "SOL/USDT", "source_id": "s1"},
        )
        prompt = self.retriever.build_rag_prompt(
            {"symbol": "SOL/USDT", "position": {"amount": 10}},
            "DCA: last trades for SOL and should we add?",
            template="dca_advice_rag",
        )
        self.assertIn("RETRIEVED_MEMORY", prompt)
        self.assertIn("SOL", prompt)
        self.assertIn("DCA", prompt)

    def test_disabled_rag_returns_empty(self):
        off = {"memory": {"rag": {"enabled": False}}}
        r = RagRetriever.in_memory(config=off)
        cid = r.add_to_memory("x", {"type": "lesson"})
        self.assertEqual(cid, "")
        self.assertEqual(r.retrieve("x"), [])

    def test_retrieve_fail_open_on_store_error(self):
        class BoomStore:
            def list_chunks(self, *, limit=500):
                raise RuntimeError("mongo down")

            def upsert_chunk(self, chunk):
                raise RuntimeError("mongo down")

        r = RagRetriever(store=BoomStore(), config=self.cfg)
        self.assertEqual(r.retrieve("anything"), [])
        self.assertEqual(r.add_to_memory("t", {"type": "x"}), "")

    def test_rank_prefers_higher_cosine(self):
        from intelligence.memory.embeddings import embed_text
        from intelligence.memory.rag_store import RagChunk

        q = "bitcoin funding risk off"
        a = RagChunk("1", q + " extreme funding crowded long", embed_text(q + " funding"), {"type": "event"})
        b = RagChunk("2", "unrelated banana recipe", embed_text("banana recipe"), {"type": "event"})
        ranked = rank_chunks(q, [b, a], top_k=2)
        self.assertEqual(ranked[0][1].chunk_id, "1")


class TestRagStoreSafety(unittest.TestCase):
    def test_collection_name_is_memory_prefixed(self):
        from intelligence.memory import rag_store as rs

        self.assertTrue(rs.COL_RAG.startswith("memory_"))
        self.assertNotIn(rs.COL_RAG, rs._FORBIDDEN)


class TestRagSafetyNoExecImports(unittest.TestCase):
    def test_rag_modules_do_not_reference_order_writers(self):
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[2]
        paths = [
            root / "hermes/memory/rag_retriever.py",
            root / "intelligence/memory/rag_store.py",
            root / "intelligence/memory/rag_index.py",
            root / "intelligence/memory/rag_config.py",
        ]
        banned = (
            "create_market_buy",
            "create_market_sell",
            "execute_order",
            "init_position",
            "record_trade",
            "record_order_fill",
        )
        for p in paths:
            text = p.read_text(encoding="utf-8")
            for b in banned:
                self.assertNotIn(b, text, msg=f"{p.name} must not call {b}")


if __name__ == "__main__":
    unittest.main()
