"""HTTP server: static Three.js UI + read-only JSON APIs.

Usage:
  python -m tools.memory_viz.server
  MEMORY_VIZ_DEMO=1 PORT=8765 python -m tools.memory_viz.server
"""

from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from tools.memory_viz.store import LEDGER_COLLECTIONS, get_store

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _env_bool(name: str, default: bool = False) -> bool:
    v = (os.environ.get(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def ensure_store_loaded() -> None:
    store = get_store()
    if store.node_count > 0:
        return
    demo = _env_bool("MEMORY_VIZ_DEMO", True)
    cortex_path = (os.environ.get("MEMORY_VIZ_CORTEX") or "").strip()
    if cortex_path and Path(cortex_path).is_file():
        store.load_json(cortex_path)
    elif demo:
        store.load_demo()
    else:
        store.load_demo()


class CortexHandler(BaseHTTPRequestHandler):
    server_version = "MemoryCortex/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        # quieter default; still useful for local debug
        if os.environ.get("MEMORY_VIZ_VERBOSE"):
            super().log_message(fmt, *args)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        # CORS not required for same-origin; allow simple local probes
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
        ensure_store_loaded()
        parsed = urlparse(self.path)
        path = unquote(parsed.path or "/")

        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/") :])
        # allow direct asset paths used by index.html
        if path in ("/css/cortex.css", "/js/main.js", "/js/scene.js", "/js/hud.js"):
            return self._static(path.lstrip("/"))

        if path == "/api/health":
            h = get_store().health()
            h["ledger_collections_blocked"] = sorted(LEDGER_COLLECTIONS)
            return self._json(200, h)
        if path == "/api/cortex":
            return self._json(200, get_store().public_cortex())
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
        if path != "/api/query":
            return self._json(404, {"error": "not_found"})
        body = self._read_json()
        q = str(body.get("query") or body.get("q") or "")
        try:
            top_k = int(body.get("top_k") or body.get("k") or 40)
        except (TypeError, ValueError):
            top_k = 40
        top_k = max(1, min(top_k, 100))
        return self._json(200, get_store().query(q, top_k=top_k))

    def _static(self, rel: str) -> None:
        # prevent path traversal
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
        elif rel.endswith(".svg"):
            ctype = "image/svg+xml"
        self._send(200, data, ctype)


def run(host: str | None = None, port: int | None = None) -> None:
    ensure_store_loaded()
    host = host or (os.environ.get("MEMORY_VIZ_HOST") or "0.0.0.0").strip()
    if port is None:
        port = int(os.environ.get("PORT") or os.environ.get("MEMORY_VIZ_PORT") or "8765")
    httpd = ThreadingHTTPServer((host, port), CortexHandler)
    store = get_store()
    print(
        f"Memory Cortex online http://{host}:{port} "
        f"nodes={store.node_count} demo={store.is_demo}",
        flush=True,
    )
    httpd.serve_forever()


def main() -> None:
    run()


if __name__ == "__main__":
    main()
