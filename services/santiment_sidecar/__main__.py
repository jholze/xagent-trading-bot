"""Entrypoint: health server + poll loop.

  python -m services.santiment_sidecar
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

# Allow `python -m services.santiment_sidecar` from repo root.
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from services.santiment_sidecar.client import SantimentClient
from services.santiment_sidecar.config import load_config
from services.santiment_sidecar.publisher import publish_snapshot
from services.santiment_sidecar.regime import should_push
from services.santiment_sidecar.snapshot import build_snapshot

log = logging.getLogger("santiment_sidecar")

_STATE = {
    "last_snapshot": None,
    "last_push_at": 0.0,
    "last_error": "",
    "polls": 0,
    "pushes": 0,
    "next_backoff_sec": None,
}


class _HealthHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A003
        return

    def do_GET(self):  # noqa: N802
        if self.path not in ("/health", "/", "/health/detail"):
            self.send_response(404)
            self.end_headers()
            return
        snap = _STATE["last_snapshot"] or {}
        meta = snap.get("meta") if isinstance(snap.get("meta"), dict) else {}
        body = {
            "status": "OK",
            "service": "xagent-santiment",
            "polls": _STATE["polls"],
            "pushes": _STATE["pushes"],
            "last_error": _STATE["last_error"],
            "last_regime": snap.get("regime"),
            "last_as_of": snap.get("as_of"),
            "size_mult": snap.get("size_mult"),
            "data_lag_days_max": meta.get("data_lag_days_max"),
            "metrics_ok": meta.get("metrics_ok") or [],
            "metrics_failed": meta.get("metrics_failed") or [],
            "policy_inputs": meta.get("policy_inputs") or [],
            "social_fresh": meta.get("social_fresh"),
            "scores": snap.get("scores") or {},
            "rationale": snap.get("rationale"),
        }
        raw = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def _start_health_server(port: int) -> None:
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True, name="health")
    t.start()
    log.info("health server on :%s", port)


def run_once(cfg: dict, client: SantimentClient) -> dict:
    """One poll cycle. Returns snapshot; sets _STATE['next_backoff_sec'] on 429."""
    _STATE["next_backoff_sec"] = None
    if client.available():
        fetched = client.fetch_features()
        features = fetched.features
        meta = fetched.meta
    else:
        log.warning("no SANTIMENT_API_KEY — neutral snapshot")
        features = {}
        meta = {
            "data_lag_days_max": None,
            "metrics_ok": [],
            "metrics_failed": ["no_api_key"],
            "policy_inputs": [],
            "social_fresh": False,
            "lagged_excluded_from_policy": True,
        }
    if meta.get("rate_limited"):
        retry = meta.get("rate_limit_retry_sec")
        try:
            retry_f = float(retry) if retry is not None else None
        except (TypeError, ValueError):
            retry_f = None
        # Cap insane "3 weeks" waits from monthly exhaustion — keep polling later.
        max_backoff = float(cfg.get("rate_limit_backoff_sec") or 7200)
        if retry_f is not None and retry_f > max_backoff:
            log.warning(
                "Santiment suggested retry_after=%.0fs — using max backoff %.0fs",
                retry_f,
                max_backoff,
            )
            retry_f = max_backoff
        _STATE["next_backoff_sec"] = retry_f or max_backoff
    snap = build_snapshot(
        features,
        meta=meta,
        schema_version=cfg["schema_version"],
        ttl_sec=cfg["ttl_sec"],
    )
    _STATE["polls"] += 1
    _STATE["last_snapshot"] = snap
    now = time.time()
    heartbeat_due = (now - float(_STATE["last_push_at"] or 0)) >= cfg["heartbeat_sec"]
    if should_push(
        None if _STATE["pushes"] == 0 else _STATE.get("_prev_pushed"),
        snap,
        size_delta=cfg["size_delta_push"],
        heartbeat_due=heartbeat_due or _STATE["pushes"] == 0,
    ):
        ok, msg = publish_snapshot(
            snap,
            url=cfg["bot_ingest_url"],
            token=cfg["bot_ingest_token"],
            dry_run=cfg["dry_run"] or not cfg["bot_ingest_url"],
        )
        if ok:
            _STATE["pushes"] += 1
            _STATE["last_push_at"] = now
            _STATE["_prev_pushed"] = snap
            _STATE["last_error"] = ""
            log.info(
                "pushed regime=%s size_mult=%s conf=%.2f lag=%s ok=%s profile=%s (%s)",
                snap["regime"],
                snap["size_mult"],
                snap["confidence"],
                (snap.get("meta") or {}).get("data_lag_days_max"),
                len((snap.get("meta") or {}).get("metrics_ok") or []),
                (snap.get("meta") or {}).get("metric_profile"),
                msg,
            )
        else:
            _STATE["last_error"] = msg
            log.warning("push failed: %s", msg)
    else:
        log.info("no push (unchanged) regime=%s", snap.get("regime"))
    return snap


def main() -> None:
    cfg = load_config()
    logging.basicConfig(
        level=getattr(logging, cfg["log_level"], logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    log.info(
        "starting santiment sidecar poll=%ss heartbeat=%ss profile=%s delay=%.2fs "
        "dry_run=%s ingest=%s",
        cfg["poll_interval_sec"],
        cfg["heartbeat_sec"],
        cfg.get("metric_profile"),
        cfg.get("inter_request_delay_sec"),
        cfg["dry_run"],
        bool(cfg["bot_ingest_url"]),
    )
    _start_health_server(cfg["port"])
    client = SantimentClient(
        cfg["api_key"],
        inter_request_delay_sec=float(cfg.get("inter_request_delay_sec") or 0),
        abort_on_rate_limit=bool(cfg.get("abort_on_rate_limit", True)),
        fetch_social=bool(cfg.get("fetch_social")),
        fetch_leverage=bool(cfg.get("fetch_leverage")),
        fetch_dev=bool(cfg.get("fetch_dev")),
        leverage_research_fallback=bool(cfg.get("leverage_research_fallback")),
    )
    backoff = cfg["poll_interval_sec"]
    while True:
        try:
            run_once(cfg, client)
            extra = _STATE.get("next_backoff_sec")
            if extra is not None:
                backoff = max(cfg["poll_interval_sec"], float(extra))
                log.info("next poll in %.0fs (rate-limit backoff)", backoff)
            else:
                backoff = cfg["poll_interval_sec"]
        except Exception as e:
            _STATE["last_error"] = str(e)
            log.exception("poll cycle failed: %s", e)
            backoff = min(
                float(cfg.get("rate_limit_backoff_sec") or 7200),
                max(backoff * 2, cfg["poll_interval_sec"]),
            )
        time.sleep(backoff)


if __name__ == "__main__":
    main()
