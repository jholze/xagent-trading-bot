"""V2: WebSocket frames, ingest live path, watcher poll (no real Mongo required)."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest import mock

from tools.memory_viz.server import CortexHandler, ensure_store_loaded, reset_boot_for_tests
from tools.memory_viz.store import reset_store_for_tests
from tools.memory_viz.watcher import MemoryWatcher
from tools.memory_viz.ws_hub import decode_frames, encode_text_frame, reset_hub_for_tests, ws_accept_key


def _ws_handshake(host: str, port: int, path: str = "/ws"):
    key = "dGhlIHNhbXBsZSBub25jZQ=="  # standard example key
    sock = socket.create_connection((host, port), timeout=5)
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    sock.sendall(req.encode())
    # read headers
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    assert b"101" in head.split(b"\r\n")[0], head[:200]
    accept = None
    for line in head.decode().split("\r\n"):
        if line.lower().startswith("sec-websocket-accept:"):
            accept = line.split(":", 1)[1].strip()
    assert accept == ws_accept_key(key)
    return sock, bytearray(rest)


def _read_ws_json(sock, buf: bytearray, timeout=3.0):
    sock.settimeout(timeout)
    deadline = time.time() + timeout
    while time.time() < deadline:
        messages, buf = decode_frames(buf)
        for m in messages:
            if m is None:
                return None, buf
            try:
                return json.loads(m), buf
            except Exception:
                continue
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            continue
        if not chunk:
            break
        buf.extend(chunk)
    raise TimeoutError("no ws json")


class TestWsProtocol(unittest.TestCase):
    def test_accept_key_rfc_example(self):
        # RFC 6455 example
        self.assertEqual(
            ws_accept_key("dGhlIHNhbXBsZSBub25jZQ=="),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
        )

    def test_encode_decode_roundtrip_client_style(self):
        # server→client frames are unmasked; decode_frames supports unmasked
        frame = encode_text_frame('{"type":"hello"}')
        msgs, rem = decode_frames(bytearray(frame))
        self.assertEqual(rem, bytearray())
        self.assertEqual(json.loads(msgs[0])["type"], "hello")


class TestIngestAndWs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reset_boot_for_tests()
        reset_hub_for_tests()
        cls._env = mock.patch.dict(
            os.environ,
            {"MEMORY_VIZ_DEMO": "1", "MEMORY_VIZ_WATCHER": "0"},
            clear=False,
        )
        cls._env.start()
        reset_store_for_tests().load_demo(variants_per_seed=2)
        ensure_store_loaded()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), CortexHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        reset_boot_for_tests()
        cls._env.stop()

    def test_ws_hello_and_ingest_broadcast(self):
        sock, buf = _ws_handshake("127.0.0.1", self.port)
        try:
            hello, buf = _read_ws_json(sock, buf)
            self.assertEqual(hello["type"], "hello")
            self.assertGreater(hello["node_count"], 0)

            # ingest via HTTP while WS connected
            conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
            body = json.dumps(
                {
                    "text": "ARIA brand new volume breakout memory event for live test",
                    "metadata": {
                        "source": "cmc_pro_quotes",
                        "type": "volume_breakout",
                        "symbol": "ARIA/USDT",
                    },
                }
            ).encode()
            conn.request(
                "POST",
                "/api/ingest",
                body=body,
                headers={"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            raw = resp.read()
            conn.close()
            self.assertEqual(resp.status, 200, raw)
            ing = json.loads(raw)
            self.assertTrue(ing["ok"])
            self.assertIn("ARIA", ing["node"]["preview"] or ing["node"]["title"] or "")

            # wait for nodes_added (may need to skip ping)
            found = None
            deadline = time.time() + 5
            while time.time() < deadline:
                msg, buf = _read_ws_json(sock, buf, timeout=2)
                if not msg:
                    break
                if msg.get("type") == "nodes_added":
                    found = msg
                    break
            self.assertIsNotNone(found, "expected nodes_added over websocket")
            self.assertGreaterEqual(len(found["nodes"]), 1)
            self.assertEqual(found["nodes"][0]["id"], ing["node"]["id"])
        finally:
            sock.close()

    def test_health_reports_ws(self):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/health")
        r = conn.getresponse()
        h = json.loads(r.read())
        conn.close()
        self.assertTrue(h["ok"])
        self.assertTrue(h.get("ws"))


class TestWatcher(unittest.TestCase):
    def test_poll_once_adds_and_broadcasts(self):
        store = reset_store_for_tests()
        store.load_demo(variants_per_seed=1)
        events = []

        def fake_fetch(*, limit=200, since_created_at=None, exclude_ids=None):
            return [
                {
                    "_id": "mongo_new_1",
                    "chunk_id": "mongo_new_1",
                    "text": "ARIA fresh coin fact from mongo poll",
                    "metadata": {
                        "source": "cmc_pro_quotes",
                        "type": "volume_breakout",
                        "symbol": "ARIA/USDT",
                    },
                    "created_at": "2099-01-01T00:00:00Z",
                    "embedding": [],
                }
            ]

        w = MemoryWatcher(
            store,
            poll_sec=60,
            fetch_fn=fake_fetch,
            broadcast_fn=lambda ev: events.append(ev) or 1,
        )
        w.seed_known_from_store()
        before = store.node_count
        added = w.poll_once()
        self.assertEqual(len(added), 1)
        self.assertEqual(store.node_count, before + 1)
        self.assertEqual(events[0]["type"], "nodes_added")
        # second poll dedupes
        added2 = w.poll_once()
        self.assertEqual(added2, [])


class TestMongoSafety(unittest.TestCase):
    def test_refuses_ledger_collection(self):
        from tools.memory_viz.mongo_source import assert_collection_allowed

        with self.assertRaises(RuntimeError):
            assert_collection_allowed("orders")
        with self.assertRaises(RuntimeError):
            assert_collection_allowed("memory_evil")  # only rag_chunks
        assert_collection_allowed("memory_rag_chunks")


if __name__ == "__main__":
    unittest.main()
