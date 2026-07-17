"""Hermes + Memory background service loop (Railway xagent-hermes).

Does NOT write orders/positions. Safe for shared Mongo xagent_test.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from intelligence.memory.event_ingest import sync_fusion_events
from intelligence.memory.export import export_jsonl
from intelligence.memory.news_providers import poll_and_ingest_news
from intelligence.memory.rebuild import rebuild_from_orders
from intelligence.memory.reflector import reflect
from intelligence.memory.store import MemoryStore, memory_enabled
from intelligence.memory.vector_weaviate import WeaviateIndex, weaviate_enabled
from logger import log

_STATE: dict = {
    "last_cycle_at": 0.0,
    "last_error": "",
    "cycles": 0,
    "last_rebuild": {},
    "last_news": {},
    "last_reflect": {},
}


class _Health(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A003
        return

    def do_GET(self):  # noqa: N802
        if self.path not in ("/", "/health", "/health/detail"):
            self.send_response(404)
            self.end_headers()
            return
        body = {
            "status": "OK",
            "service": "xagent-hermes",
            "memory_enabled": memory_enabled(),
            "weaviate": weaviate_enabled(),
            "cycles": _STATE["cycles"],
            "last_error": _STATE["last_error"],
            "last_rebuild": _STATE["last_rebuild"],
            "last_news": _STATE["last_news"],
            "last_reflect": _STATE["last_reflect"],
        }
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def run_memory_cycle(store: MemoryStore | None = None) -> dict:
    """One full memory maintenance cycle — ledger read-only."""
    store = store or MemoryStore()
    store.ensure_indexes()
    out = {
        "rebuild": rebuild_from_orders(store),
        "events": sync_fusion_events(store),
        "news": {},
        "reflect": {},
        "weaviate_ready": False,
    }
    try:
        out["news"] = poll_and_ingest_news(store)
    except Exception as e:
        log(f"memory news cycle: {e}", "WARNING")
    try:
        out["reflect"] = reflect(store)
    except Exception as e:
        log(f"memory reflect cycle: {e}", "WARNING")
    if weaviate_enabled():
        try:
            idx = WeaviateIndex()
            out["weaviate_ready"] = idx.ready()
            if out["weaviate_ready"]:
                idx.ensure_schema()
                for ev in store.list_events(limit=20):
                    idx.upsert_event(
                        ev.event_id,
                        ev.description,
                        event_type=ev.event_type,
                        source=ev.source,
                        impact_score=ev.impact_score,
                        vector=ev.embedding or None,
                    )
        except Exception as e:
            log(f"weaviate cycle: {e}", "DEBUG")
    # Optional Hermes param learning cycle (heavy)
    if os.environ.get("HERMES_RUN_LEARNING", "1").strip() not in ("0", "false"):
        try:
            from hermes.agent import HermesAgent

            agent = HermesAgent()
            result = agent.run_cycle()
            out["hermes"] = {
                "symbol": result.symbol,
                "verdict": result.verdict,
                "promoted": result.promoted,
                "variable": result.variable,
            }
            log(
                f"hermes learning: {result.symbol} {result.variable} → {result.verdict}",
                "INFO",
            )
        except Exception as e:
            log(f"hermes learning cycle skipped: {e}", "WARNING")
            out["hermes_error"] = str(e)[:200]
    return out


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    port = int(os.environ.get("PORT", "8080"))
    interval = max(120, int(os.environ.get("HERMES_INTERVAL_SEC", "1800")))
    news_every = max(1, int(os.environ.get("MEMORY_NEWS_EVERY_N", "1")))

    httpd = HTTPServer(("0.0.0.0", port), _Health)
    threading.Thread(target=httpd.serve_forever, daemon=True, name="health").start()
    log(f"xagent-hermes memory service on :{port} interval={interval}s", "INFO")

    store = MemoryStore()
    n = 0
    while True:
        try:
            result = run_memory_cycle(store)
            n += 1
            _STATE["cycles"] = n
            _STATE["last_cycle_at"] = time.time()
            _STATE["last_rebuild"] = result.get("rebuild") or {}
            _STATE["last_news"] = result.get("news") or {}
            _STATE["last_reflect"] = result.get("reflect") or {}
            _STATE["last_error"] = ""
            if n % 12 == 0:  # ~ daily if 30min cycles
                try:
                    export_jsonl()
                except Exception:
                    pass
            try:
                from bus.heartbeats import heartbeat_registry

                heartbeat_registry.beat("hermes", ttl_sec=interval * 2)
            except Exception:
                pass
        except Exception as e:
            _STATE["last_error"] = str(e)[:300]
            log(f"memory service cycle error: {e}", "ERROR")
        time.sleep(interval)


if __name__ == "__main__":
    main()
