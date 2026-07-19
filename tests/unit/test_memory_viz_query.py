"""Unit tests for ranking + demo cortex query path (shipped functions)."""

from __future__ import annotations

import unittest

from tools.memory_viz.demo_cortex import build_demo_cortex
from tools.memory_viz.ranking import cosine, top_k_cosine
from tools.memory_viz.store import CortexStore, LEDGER_COLLECTIONS, reset_store_for_tests


class TestRanking(unittest.TestCase):
    def test_cosine_identical(self):
        v = [1.0, 0.0, 0.0]
        self.assertAlmostEqual(cosine(v, v), 1.0, places=5)

    def test_top_k_orders_by_score(self):
        # query along x-axis
        q = [1.0, 0.0, 0.0]
        matrix = [
            [0.0, 1.0, 0.0],  # 0
            [1.0, 0.0, 0.0],  # 1 best
            [0.7, 0.7, 0.0],  # 2 mid
        ]
        ranked = top_k_cosine(q, matrix, k=2)
        self.assertEqual(len(ranked), 2)
        self.assertEqual(ranked[0][0], 1)
        self.assertGreater(ranked[0][1], ranked[1][1])

    def test_top_k_empty(self):
        self.assertEqual(top_k_cosine([1.0], [], k=5), [])
        self.assertEqual(top_k_cosine([1.0], [[1.0]], k=0), [])


class TestDemoQuery(unittest.TestCase):
    def test_demo_builds_nonempty(self):
        cortex, vectors = build_demo_cortex(variants_per_seed=2)
        self.assertTrue(cortex["demo"])
        self.assertGreater(cortex["node_count"], 10)
        self.assertEqual(len(vectors), cortex["node_count"])
        self.assertEqual(len(vectors[0]), cortex["embedding_dim"])
        lobes = {n["lobe"] for n in cortex["nodes"]}
        for need in ("coin_facts", "trades", "lessons", "events", "social"):
            self.assertIn(need, lobes)

    def test_store_query_aria_volume_hits(self):
        store = reset_store_for_tests()
        store.load_demo(variants_per_seed=3)
        out = store.query("ARIA volume breakout", top_k=10)
        self.assertEqual(out["query"], "ARIA volume breakout")
        self.assertGreater(len(out["hits"]), 0)
        self.assertEqual(len(out["indices"]), len(out["scores"]))
        # scores numeric and non-increasing
        scores = out["scores"]
        self.assertTrue(all(isinstance(s, float) for s in scores))
        for a, b in zip(scores, scores[1:]):
            self.assertGreaterEqual(a, b - 1e-9)
        # top hit should relate to ARIA or volume in title/preview
        top = out["hits"][0]
        blob = f"{top.get('title')} {top.get('preview')} {top.get('symbol')}".upper()
        self.assertTrue(
            "ARIA" in blob or "VOLUME" in blob,
            msg=f"unexpected top hit: {top}",
        )
        # node fetch
        node = store.get_node(top["id"])
        self.assertIsNotNone(node)
        self.assertTrue(node.get("text") or node.get("preview"))

    def test_ledger_collections_blocked_constant(self):
        for name in ("orders", "positions", "trade_history"):
            self.assertIn(name, LEDGER_COLLECTIONS)
        h = CortexStore()
        h.load_demo(variants_per_seed=1)
        health = h.health()
        self.assertTrue(health["ok"])
        self.assertIn("orders", health["ledger_blocked"])


if __name__ == "__main__":
    unittest.main()
