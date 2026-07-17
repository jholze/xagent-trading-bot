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
from intelligence.memory.social_ingest import reflect_social, sync_social_memory
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
    "last_social": {},
    "last_hermes": {},
    "live_evidence": {
        "mode": "dual",
        "promotions": 0,
        "rejections": 0,
        "live_vetoes": 0,
        "cycles": 0,
    },
    "weaviate_ready": False,
}


def _live_evidence_mode() -> str:
    mode = (os.environ.get("HERMES_LIVE_EVIDENCE_MODE") or "").strip().lower()
    if mode in ("observe", "soft", "dual"):
        return mode
    try:
        from core.config import get_bot_config

        le = (get_bot_config().raw.get("hermes") or {}).get("live_evidence") or {}
        m = str(le.get("mode") or "dual").lower()
        if m in ("observe", "soft", "dual"):
            return m
    except Exception:
        pass
    return "dual"


def _record_hermes_outcome(result) -> dict:
    """Track promotion / veto rates for /health (TM-8)."""
    le = _STATE["live_evidence"]
    le["mode"] = _live_evidence_mode()
    le["cycles"] = int(le.get("cycles") or 0) + 1
    promoted = bool(getattr(result, "promoted", False))
    verdict = str(getattr(result, "verdict", "") or "")
    # agent result may not expose live_veto on CycleResult — check dict form
    live_veto = bool(getattr(result, "live_veto", False))
    if promoted:
        le["promotions"] = int(le.get("promotions") or 0) + 1
    else:
        le["rejections"] = int(le.get("rejections") or 0) + 1
    if live_veto or "live_veto" in verdict.lower() or "live veto" in verdict.lower():
        le["live_vetoes"] = int(le.get("live_vetoes") or 0) + 1
    cycles = max(1, int(le["cycles"]))
    le["promotion_rate"] = round(int(le.get("promotions") or 0) / cycles, 4)
    le["veto_rate"] = round(int(le.get("live_vetoes") or 0) / cycles, 4)
    le["reject_rate"] = round(int(le.get("rejections") or 0) / cycles, 4)
    return {
        "symbol": getattr(result, "symbol", None),
        "verdict": verdict,
        "promoted": promoted,
        "variable": getattr(result, "variable", None),
        "live_evidence_mode": le["mode"],
    }


class _Health(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A003
        return

    def do_GET(self):  # noqa: N802
        if self.path not in ("/", "/health", "/health/detail", "/hermes"):
            self.send_response(404)
            self.end_headers()
            return
        le = dict(_STATE.get("live_evidence") or {})
        body = {
            "status": "OK",
            "service": "xagent-hermes",
            "memory_enabled": memory_enabled(),
            "weaviate": weaviate_enabled(),
            "weaviate_ready": bool(_STATE.get("weaviate_ready")),
            "cycles": _STATE["cycles"],
            "last_error": _STATE["last_error"],
            "last_rebuild": _STATE["last_rebuild"],
            "last_news": _STATE["last_news"],
            "last_reflect": _STATE["last_reflect"],
            "last_social": _STATE.get("last_social") or {},
            "last_hermes": _STATE.get("last_hermes") or {},
            "live_evidence": le,
            "promotion_rate": le.get("promotion_rate", 0.0),
            "veto_rate": le.get("veto_rate", 0.0),
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
        "social": {},
        "news": {},
        "reflect": {},
        "weaviate_ready": False,
        "hermes": {},
    }
    try:
        out["social"] = sync_social_memory(store)
    except Exception as e:
        log(f"memory social cycle: {e}", "WARNING")
        out["social"] = {"error": str(e)[:200]}
    try:
        out["news"] = poll_and_ingest_news(store)
    except Exception as e:
        log(f"memory news cycle: {e}", "WARNING")
    try:
        out["reflect"] = reflect(store)
    except Exception as e:
        log(f"memory reflect cycle: {e}", "WARNING")
    try:
        social_ref = reflect_social(store)
        if isinstance(out.get("reflect"), dict):
            out["reflect"] = {**out["reflect"], **social_ref}
        else:
            out["reflect"] = social_ref
    except Exception as e:
        log(f"memory social reflect: {e}", "WARNING")
    if weaviate_enabled():
        try:
            idx = WeaviateIndex()
            out["weaviate_ready"] = idx.ready()
            _STATE["weaviate_ready"] = out["weaviate_ready"]
            if out["weaviate_ready"]:
                idx.ensure_schema()
                for ev in store.list_events(limit=40):
                    idx.upsert_event(
                        ev.event_id,
                        ev.description,
                        event_type=ev.event_type,
                        source=ev.source,
                        impact_score=ev.impact_score,
                        symbols=ev.symbols,
                        timestamp=ev.timestamp,
                        vector=ev.embedding or None,
                    )
                for prof in store.list_profiles(limit=40):
                    idx.upsert_profile(
                        prof.symbol,
                        rationale=prof.rationale,
                        size_bias=prof.size_bias,
                        risk_score=prof.risk_score,
                        entry_bias=prof.entry_bias,
                        ledger_scope=prof.ledger_scope,
                        as_of=prof.as_of,
                        vector=prof.embedding or None,
                    )
                for tr in store.list_trades(limit=30):
                    idx.upsert_trade(
                        tr.trade_id,
                        tr.symbol,
                        outcome=tr.outcome,
                        source=tr.source,
                        pnl_usdt=float(tr.pnl_usdt or 0),
                        reason=tr.reason,
                        vector=tr.embedding or None,
                    )
                for les in store.list_lessons(limit=30):
                    idx.upsert_lesson(
                        les.lesson_id,
                        les.text,
                        confidence=les.confidence,
                        tags=les.tags,
                        symbols=les.symbols,
                        validated=les.validated,
                        vector=les.embedding or None,
                    )
        except Exception as e:
            log(f"weaviate cycle: {e}", "DEBUG")
            out["weaviate_ready"] = False
            _STATE["weaviate_ready"] = False

    # Optional Hermes param learning cycle (heavy)
    # live_evidence modes: observe = track only; soft/dual = full guardrails in agent
    mode = _live_evidence_mode()
    run_learning = os.environ.get("HERMES_RUN_LEARNING", "1").strip() not in ("0", "false")
    if run_learning:
        try:
            from hermes.agent import HermesAgent

            agent = HermesAgent()
            # observe: still run cycle but agent config may not promote destructively;
            # promotion tracking always recorded for /health rates
            result = agent.run_cycle()
            out["hermes"] = _record_hermes_outcome(result)
            if mode == "observe":
                out["hermes"]["note"] = "observe_mode_learning_tracked"
            log(
                f"hermes learning: {result.symbol} {result.variable} → {result.verdict} "
                f"(promoted={result.promoted} mode={mode})",
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

    httpd = HTTPServer(("0.0.0.0", port), _Health)
    threading.Thread(target=httpd.serve_forever, daemon=True, name="health").start()
    log(
        f"xagent-hermes memory service on :{port} interval={interval}s "
        f"live_evidence={_live_evidence_mode()} weaviate={weaviate_enabled()}",
        "INFO",
    )

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
            _STATE["last_social"] = result.get("social") or {}
            _STATE["last_hermes"] = result.get("hermes") or {}
            _STATE["weaviate_ready"] = bool(result.get("weaviate_ready"))
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
