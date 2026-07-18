"""Epic #72 C8: optional fusion snapshot → RAG (default off)."""

from __future__ import annotations

import unittest

from hermes.memory.rag_retriever import RagRetriever
from intelligence.memory.rag_index import index_fusion_snapshot


class TestFusionSnapshotIndex(unittest.TestCase):
    def test_default_off_no_index(self):
        r = RagRetriever.in_memory(
            config={
                "memory": {
                    "rag": {
                        "enabled": True,
                        "index_market_context": False,
                        "embedding_backend": "hash",
                    }
                }
            }
        )
        cid = index_fusion_snapshot(
            {
                "regime": "RISK_OFF",
                "size_mult": 0.35,
                "sensor_policy": "shadow",
                "source": "oracle",
                "block_buys": False,
                "rationale": "test",
            },
            retriever=r,
            config={
                "memory": {
                    "rag": {"enabled": True, "index_market_context": False}
                }
            },
        )
        self.assertEqual(cid, "")
        self.assertEqual(r.retrieve("RISK_OFF fusion size"), [])

    def test_flag_on_indexes_retrievable_chunk(self):
        cfg = {
            "memory": {
                "rag": {
                    "enabled": True,
                    "index_market_context": True,
                    "embedding_backend": "hash",
                    "top_k": 5,
                }
            }
        }
        r = RagRetriever.in_memory(config=cfg)
        cid = index_fusion_snapshot(
            {
                "active": True,
                "regime": "RISK_OFF",
                "size_mult": 0.35,
                "sensor_policy": "shadow",
                "source": "oracle,santiment",
                "block_buys": True,
                "warmup_active": False,
                "rationale": "breadth weak unique_fusion_marker",
            },
            retriever=r,
            config=cfg,
        )
        self.assertTrue(cid)
        hits = r.retrieve("fusion RISK_OFF size cut breadth", top_k=3)
        self.assertTrue(hits)
        blob = " ".join(h.text for h in hits)
        self.assertIn("unique_fusion_marker", blob)
        self.assertIn("RISK_OFF", blob)
        self.assertIn("0.35", blob)

    def test_empty_bias_noop(self):
        r = RagRetriever.in_memory(
            config={"memory": {"rag": {"enabled": True, "index_market_context": True}}}
        )
        self.assertEqual(index_fusion_snapshot(None, retriever=r), "")
        self.assertEqual(index_fusion_snapshot({}, retriever=r), "")


if __name__ == "__main__":
    unittest.main()
