"""Epic #72 C5: MemoryRagChunk dim 384 — schema + dual-write/search fail-open."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from hermes.memory.rag_retriever import RagRetriever
from intelligence.memory.embeddings import embed_for_rag, rag_embedding_dim
from intelligence.memory.vector_weaviate import (
    CLASS_RAG,
    VECTOR_DIM_RAG,
    WeaviateIndex,
    _normalize_vector,
)


class TestRagEmbeddingDim(unittest.TestCase):
    def test_embed_for_rag_is_384(self):
        self.assertEqual(rag_embedding_dim(), 384)
        self.assertEqual(VECTOR_DIM_RAG, 384)
        v = embed_for_rag("ARIA DCA stop loss after volume spike")
        self.assertEqual(len(v), 384)

    def test_normalize_rag_vector_dim(self):
        v = _normalize_vector(None, "hello rag chunk", dim=VECTOR_DIM_RAG)
        self.assertEqual(len(v), 384)


class TestWeaviateRagApi(unittest.TestCase):
    def test_upsert_and_search_called_with_class_and_dim(self):
        idx = WeaviateIndex(base_url="http://weaviate.test")
        with patch.object(idx, "ensure_schema") as ens, patch.object(
            idx, "_upsert", return_value=True
        ) as up, patch.object(idx, "ready", return_value=True):
            ok = idx.upsert_rag_chunk(
                "rag_abc",
                "Trade ARIA/USDT sell pnl=-50",
                chunk_type="trade",
                symbol="ARIA/USDT",
                vector=embed_for_rag("Trade ARIA/USDT sell pnl=-50"),
            )
        self.assertTrue(ok)
        ens.assert_called()
        self.assertEqual(up.call_args[0][0], CLASS_RAG)
        self.assertEqual(up.call_args[1].get("vector_dim"), VECTOR_DIM_RAG)

    def test_search_rag_parses_graphql_rows(self):
        idx = WeaviateIndex(base_url="http://weaviate.test")
        fake = {
            "data": {
                "Get": {
                    CLASS_RAG: [
                        {
                            "chunk_id": "rag_1",
                            "text": "Trade ARIA/USDT unique_wv_hit",
                            "chunk_type": "trade",
                            "symbol": "ARIA/USDT",
                            "source": "test",
                            "_additional": {"distance": 0.1},
                        }
                    ]
                }
            }
        }
        with patch.object(idx, "ensure_schema"), patch.object(idx, "_req", return_value=fake):
            rows = idx.search_rag_chunks("ARIA DCA", k=3, symbol="ARIA/USDT")
        self.assertEqual(len(rows), 1)
        self.assertIn("unique_wv_hit", rows[0]["text"])


class TestRetrieverWeaviatePath(unittest.TestCase):
    def test_retrieve_merges_weaviate_and_mongo(self):
        r = RagRetriever.in_memory(
            config={
                "memory": {
                    "rag": {
                        "enabled": True,
                        "use_weaviate_rag": True,
                        "embedding_backend": "hash",
                    }
                }
            }
        )
        # Mongo has ETH
        r.add_to_memory(
            "Trade ETH/USDT buy unique_mongo_eth",
            {"type": "trade", "symbol": "ETH/USDT", "source_id": "e1"},
        )
        from hermes.memory.rag_retriever import RagHit

        with patch.object(
            r,
            "_weaviate_retrieve",
            return_value=[
                RagHit(
                    text="Trade ARIA/USDT from weaviate unique_wv",
                    score=0.9,
                    metadata={"type": "trade", "symbol": "ARIA/USDT"},
                    chunk_id="wv1",
                )
            ],
        ):
            hits = r.retrieve("trade", top_k=5)
        blob = " ".join(h.text for h in hits)
        self.assertIn("unique_wv", blob)
        self.assertIn("unique_mongo_eth", blob)

    def test_retrieve_falls_back_mongo_when_weaviate_empty(self):
        r = RagRetriever.in_memory(
            config={
                "memory": {
                    "rag": {
                        "enabled": True,
                        "use_weaviate_rag": True,
                        "embedding_backend": "hash",
                    }
                }
            }
        )
        r.add_to_memory(
            "Trade SOL/USDT dca unique_mongo_only",
            {"type": "trade", "symbol": "SOL/USDT", "source_id": "s1"},
        )
        with patch.object(r, "_weaviate_retrieve", return_value=[]):
            hits = r.retrieve("SOL dca", top_k=3)
        self.assertTrue(hits)
        self.assertIn("unique_mongo_only", hits[0].text)


if __name__ == "__main__":
    unittest.main()
