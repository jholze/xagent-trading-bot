"""HTTP integration tests against the real CortexHandler (demo data)."""

from __future__ import annotations

import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

from tools.memory_viz.server import CortexHandler, ensure_store_loaded
from tools.memory_viz.store import reset_store_for_tests


class TestMemoryVizServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reset_store_for_tests().load_demo(variants_per_seed=3)
        ensure_store_loaded()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), CortexHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _req(self, method: str, path: str, body: dict | None = None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        raw = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=raw, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data

    def test_health_and_cortex(self):
        code, data = self._req("GET", "/api/health")
        self.assertEqual(code, 200)
        h = json.loads(data)
        self.assertTrue(h["ok"])
        self.assertGreater(h["node_count"], 0)
        self.assertTrue(h["demo"])
        self.assertIn("orders", h.get("ledger_blocked") or h.get("ledger_collections_blocked") or [])

        code, data = self._req("GET", "/api/cortex")
        self.assertEqual(code, 200)
        c = json.loads(data)
        self.assertGreater(len(c["nodes"]), 0)
        self.assertIn("lobes", c)
        # no giant embedding blobs in public cortex
        self.assertNotIn("vectors", c)

    def test_query_scores(self):
        code, data = self._req("POST", "/api/query", {"query": "ARIA volume", "top_k": 8})
        self.assertEqual(code, 200)
        out = json.loads(data)
        self.assertGreater(len(out["hits"]), 0)
        self.assertIsInstance(out["hits"][0]["score"], (int, float))
        self.assertGreaterEqual(out["hits"][0]["score"], out["hits"][-1]["score"] - 1e-9)

    def test_index_has_threejs_and_hud(self):
        code, data = self._req("GET", "/")
        self.assertEqual(code, 200)
        html = data.decode("utf-8")
        self.assertIn("three", html.lower())
        self.assertIn("query-input", html)
        self.assertIn("hit-strip", html)
        self.assertIn("detail-drawer", html)
        self.assertIn("lobe-toggles", html)
        self.assertIn("/js/main.js", html)

        code, js = self._req("GET", "/js/scene.js")
        self.assertEqual(code, 200)
        text = js.decode("utf-8")
        self.assertIn("from \"three\"", text)
        self.assertNotIn("module.exports", text)
        self.assertNotIn("require(", text)


if __name__ == "__main__":
    unittest.main()
