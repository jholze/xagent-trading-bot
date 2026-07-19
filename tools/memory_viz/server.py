"""HTTP + WebSocket server: static UI, JSON APIs, live memory stream.

Usage:
  python -m tools.memory_viz.server
  MEMORY_VIZ_DEMO=1 PORT=8765 python -m tools.memory_viz.server
  # with Mongo (V2):
  MONGO_URL=... MONGODB_DB=xagent_test MEMORY_VIZ_DEMO=0 python -m tools.memory_viz.server
"""

from __future__ import annotations

import json
import os
import re
import select
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from tools.memory_viz.mongo_source import mongo_configured
from tools.memory_viz.store import LEDGER_COLLECTIONS, get_store
from tools.memory_viz.watcher import start_watcher, stop_watcher, get_watcher
from tools.memory_viz.ws_hub import (
    decode_frames,
    encode_text_frame,
    get_hub,
    ws_accept_key,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
_BOOTED = False
_BOOT_LOCK = threading.Lock()


def _env_bool(name: str, default: bool = False) -> bool:
    v = (os.environ.get(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def reset_boot_for_tests() -> None:
    global _BOOTED
    with _BOOT_LOCK:
        _BOOTED = False
        stop_watcher()


def ensure_store_loaded() -> None:
    global _BOOTED
    with _BOOT_LOCK:
        store = get_store()
        if _BOOTED and store.node_count > 0:
            return
        demo_pref = _env_bool("MEMORY_VIZ_DEMO", not mongo_configured())
        cortex_path = (os.environ.get("MEMORY_VIZ_CORTEX") or "").strip()
        loaded = False
        if cortex_path and Path(cortex_path).is_file():
            store.load_json(cortex_path)
            loaded = store.node_count > 0
        if not loaded and mongo_configured() and not demo_pref:
            try:
                loaded = store.load_from_mongo()
            except Exception as e:
                print(f"memory_viz mongo load failed: {e}", flush=True)
                loaded = False
        if not loaded:
            store.load_demo()
        if mongo_configured() and _env_bool("MEMORY_VIZ_WATCHER", True):
            start_watcher(store)
        _BOOTED = True


def _broadcast_nodes_added(
    nodes: list[dict[str, Any]],
    *,
    links: list[dict[str, Any]] | None = None,
) -> None:
    if not nodes:
        return
    store = get_store()
    get_hub().broadcast(
        {
            "type": "nodes_added",
            "nodes": nodes,
            "links": links or [],
            "node_count": store.node_count,
            "revision": store.revision,
        }
    )


class CortexHandler(BaseHTTPRequestHandler):
    server_version = "MemoryCortex/0.2"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        if os.environ.get("MEMORY_VIZ_VERBOSE"):
            super().log_message(fmt, *args)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: Any) -> None:
        raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send(code, raw, "application/json; charset=utf-8")

    def _read_json(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path or "/")

        # WebSocket upgrade
        if path in ("/ws", "/api/ws"):
            if (self.headers.get("Upgrade") or "").lower() == "websocket":
                return self._websocket_loop()
            return self._json(400, {"error": "expected_websocket_upgrade"})

        ensure_store_loaded()

        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/") :])
        if path in (
            "/css/cortex.css",
            "/js/main.js",
            "/js/scene.js",
            "/js/scene_graph.js",
            "/js/hud.js",
        ):
            return self._static(path.lstrip("/"))

        if path == "/api/health":
            h = get_store().health()
            h["ledger_collections_blocked"] = sorted(LEDGER_COLLECTIONS)
            h["mongo_configured"] = mongo_configured()
            h["modes"] = ["cortex", "graph"]
            w = get_watcher()
            h["watcher"] = bool(w)
            h["ws_clients"] = get_hub().client_count()
            if w:
                h["watcher_polls"] = w.polls
                h["watcher_added"] = w.added_total
            return self._json(200, h)
        if path == "/api/cortex":
            return self._json(200, get_store().public_cortex())
        if path == "/api/graph":
            # optional query knn / min_sim
            from urllib.parse import parse_qs

            qs = parse_qs(parsed.query or "")
            try:
                knn = int((qs.get("knn") or ["5"])[0])
            except (TypeError, ValueError):
                knn = 5
            try:
                min_sim = float((qs.get("min_sim") or ["0.12"])[0])
            except (TypeError, ValueError):
                min_sim = 0.12
            knn = max(2, min(knn, 16))
            min_sim = max(0.0, min(min_sim, 0.9))
            return self._json(
                200,
                get_store().public_graph(knn=knn, min_sim=min_sim),
            )
        m = re.fullmatch(r"/api/node/([^/]+)", path)
        if m:
            node = get_store().get_node(m.group(1))
            if not node:
                return self._json(404, {"error": "not_found"})
            return self._json(200, node)

        return self._json(404, {"error": "not_found", "path": path})

    def do_POST(self) -> None:  # noqa: N802
        ensure_store_loaded()
        parsed = urlparse(self.path)
        path = unquote(parsed.path or "/")
        body = self._read_json()

        if path == "/api/query":
            q = str(body.get("query") or body.get("q") or "")
            try:
                top_k = int(body.get("top_k") or body.get("k") or 40)
            except (TypeError, ValueError):
                top_k = 40
            top_k = max(1, min(top_k, 100))
            return self._json(200, get_store().query(q, top_k=top_k))

        # Live inject — always available in demo; with mongo if MEMORY_VIZ_ALLOW_INGEST=1
        if path == "/api/ingest":
            store = get_store()
            allow = store.is_demo or _env_bool("MEMORY_VIZ_ALLOW_INGEST", True)
            if not allow:
                return self._json(403, {"error": "ingest_disabled"})
            text = str(body.get("text") or body.get("body") or "")
            meta = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
            if body.get("symbol"):
                meta = {**meta, "symbol": body.get("symbol")}
            if body.get("type") and "type" not in meta:
                meta = {**meta, "type": body.get("type")}
            if body.get("source") and "source" not in meta:
                meta = {**meta, "source": body.get("source")}
            node = store.ingest_text(text, metadata=meta, node_id=body.get("id"))
            if not node:
                return self._json(400, {"error": "ingest_failed"})
            pub = {
                "i": node.get("i"),
                "id": node.get("id"),
                "pos": node.get("pos"),
                "col": node.get("col"),
                "lobe": node.get("lobe"),
                "symbol": node.get("symbol"),
                "source": node.get("source"),
                "type": node.get("type"),
                "title": node.get("title"),
                "preview": node.get("preview"),
                "created_at": node.get("created_at"),
                "nbs": node.get("nbs") or [],
            }
            links = store.graph_links_for_id(str(pub["id"]))
            _broadcast_nodes_added([pub], links=links)
            return self._json(
                200,
                {
                    "ok": True,
                    "node": pub,
                    "links": links,
                    "node_count": store.node_count,
                },
            )

        return self._json(404, {"error": "not_found"})

    def _websocket_loop(self) -> None:
        ensure_store_loaded()
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            return self._json(400, {"error": "missing_sec_websocket_key"})
        accept = ws_accept_key(key)
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()

        sock = self.connection
        hub = get_hub()
        hub.add(sock)
        # hello + snapshot meta
        store = get_store()
        hello = encode_text_frame(
            json.dumps(
                {
                    "type": "hello",
                    "node_count": store.node_count,
                    "revision": store.revision,
                    "demo": store.is_demo,
                    "source": store.health().get("source"),
                },
                separators=(",", ":"),
            )
        )
        try:
            sock.sendall(hello)
        except Exception:
            hub.remove(sock)
            return

        buf = bytearray()
        try:
            while True:
                r, _, _ = select.select([sock], [], [], 30.0)
                if not r:
                    # keep-alive ping as text (simple)
                    try:
                        sock.sendall(encode_text_frame('{"type":"ping"}'))
                    except Exception:
                        break
                    continue
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf.extend(chunk)
                messages, buf = decode_frames(buf)
                for msg in messages:
                    if msg is None:
                        return
                    # client may send {type:subscribe} — ignore, already subscribed
        except Exception:
            pass
        finally:
            hub.remove(sock)
            try:
                sock.close()
            except Exception:
                pass

    def _static(self, rel: str) -> None:
        rel = rel.replace("\\", "/").lstrip("/")
        if ".." in rel.split("/"):
            return self._json(400, {"error": "bad_path"})
        target = (STATIC_DIR / rel).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())):
            return self._json(400, {"error": "bad_path"})
        if not target.is_file():
            return self._json(404, {"error": "missing_static", "file": rel})
        data = target.read_bytes()
        ctype = "application/octet-stream"
        if rel.endswith(".html"):
            ctype = "text/html; charset=utf-8"
        elif rel.endswith(".css"):
            ctype = "text/css; charset=utf-8"
        elif rel.endswith(".js"):
            ctype = "application/javascript; charset=utf-8"
        elif rel.endswith(".json"):
            ctype = "application/json; charset=utf-8"
        self._send(200, data, ctype)


def run(host: str | None = None, port: int | None = None) -> None:
    ensure_store_loaded()
    host = host or (os.environ.get("MEMORY_VIZ_HOST") or "0.0.0.0").strip()
    if port is None:
        port = int(os.environ.get("PORT") or os.environ.get("MEMORY_VIZ_PORT") or "8765")
    httpd = ThreadingHTTPServer((host, port), CortexHandler)
    # allow reuse
    httpd.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    store = get_store()
    print(
        f"Memory Cortex online http://{host}:{port} "
        f"nodes={store.node_count} demo={store.is_demo} "
        f"mongo={mongo_configured()} ws=/ws",
        flush=True,
    )
    try:
        httpd.serve_forever()
    finally:
        stop_watcher()


def main() -> None:
    run()


if __name__ == "__main__":
    main()
