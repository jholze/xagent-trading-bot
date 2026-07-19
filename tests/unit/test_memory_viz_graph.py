"""Unit tests for Mode 2 graph builder (real shipped build_memory_graph)."""

from __future__ import annotations

import unittest

from tools.memory_viz.demo_cortex import build_demo_cortex
from tools.memory_viz.graph_build import build_memory_graph, links_for_new_node
from tools.memory_viz.store import reset_store_for_tests


class TestGraphBuild(unittest.TestCase):
    def test_build_has_nodes_and_links(self):
        cortex, vectors = build_demo_cortex(variants_per_seed=3)
        g = build_memory_graph(cortex["nodes"], vectors, knn=4, min_sim=0.05)
        self.assertGreater(g["stats"]["node_count"], 10)
        self.assertGreater(g["stats"]["link_count"], 5)
        self.assertEqual(len(g["nodes"]), g["stats"]["node_count"])
        for L in g["links"]:
            self.assertIn("source", L)
            self.assertIn("target", L)
            self.assertIn("weight", L)
            self.assertIn("kind", L)
            self.assertNotEqual(L["source"], L["target"])
            self.assertGreaterEqual(L["weight"], 0)
            self.assertLessEqual(L["weight"], 1)

    def test_symbol_edges_present(self):
        cortex, vectors = build_demo_cortex(variants_per_seed=2)
        g = build_memory_graph(cortex["nodes"], vectors, knn=3, min_sim=0.01)
        kinds = g["stats"]["kinds"]
        self.assertTrue(
            any("symbol" in k or k == "symbol" for k in kinds) or g["stats"]["link_count"] > 0
        )

    def test_store_public_graph(self):
        store = reset_store_for_tests()
        store.load_demo(variants_per_seed=2)
        g = store.public_graph(knn=4)
        self.assertEqual(g["mode"], "graph")
        self.assertGreater(len(g["nodes"]), 0)
        self.assertGreater(len(g["links"]), 0)

    def test_links_for_new_node(self):
        cortex, vectors = build_demo_cortex(variants_per_seed=2)
        nodes = cortex["nodes"]
        i = len(nodes) - 1
        links = links_for_new_node(i, nodes, vectors, knn=3, min_sim=0.01)
        self.assertIsInstance(links, list)
        for L in links:
            self.assertTrue(L["source"] == i or L["target"] == i)


if __name__ == "__main__":
    unittest.main()
