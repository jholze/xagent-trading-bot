"""Entrypoint: health server + poll loop.

  python -m services.market_oracle
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

from services.market_oracle.client import MarketDataClient
from services.market_oracle.config import load_config
from services.market_oracle.publisher import publish_snapshot
from services.market_oracle.regime import StateHysteresis, decide, should_push
from services.market_oracle.snapshot import build_snapshot

log = logging.getLogger("market_oracle")

_STATE = {
    "last_snapshot": None,
    "last_push_at": 0.0,
    "last_error": "",
    "polls": 0,
    "pushes": 0,
    "prev_pushed": None,
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
        body = {
            "status": "OK",
            "service": "xagent-market-oracle",
            "polls": _STATE["polls"],
            "pushes": _STATE["pushes"],
            "last_error": _STATE["last_error"],
            "last_state": snap.get("state") or snap.get("regime"),
            "last_as_of": snap.get("as_of"),
            "size_mult": snap.get("size_mult"),
        }
        raw = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def _start_health(port: int) -> None:
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True, name="health").start()
    log.info("health server on :%s", port)


def run_once(cfg: dict, client: MarketDataClient, hyst: StateHysteresis) -> dict:
    features = client.fetch_features()
    prev_state = (_STATE.get("prev_pushed") or {}).get("state")
    decision = decide(
        features,
        hyst,
        risk_off_24h=cfg["btc_risk_off_24h_pct"],
        crash_24h=cfg["btc_crash_24h_pct"],
        risk_on_24h=cfg["btc_risk_on_24h_pct"],
        cascade_1h=cfg["btc_cascade_1h_pct"],
        risk_on_1h_floor=cfg["btc_risk_on_1h_floor_pct"],
        risk_off_size=cfg["risk_off_size_mult"],
        neutral_size=cfg["neutral_size_mult"],
    )
    snap = build_snapshot(
        features,
        decision,
        schema_version=cfg["schema_version"],
        ttl_sec=cfg["ttl_sec"],
        previous_state=prev_state,
    )
    _STATE["polls"] += 1
    _STATE["last_snapshot"] = snap
    now = time.time()
    heartbeat_due = (now - float(_STATE["last_push_at"] or 0)) >= cfg["heartbeat_sec"]
    if should_push(
        _STATE.get("prev_pushed"),
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
            _STATE["prev_pushed"] = snap
            _STATE["last_error"] = ""
            log.info(
                "pushed state=%s size_mult=%s conf=%.2f (%s)",
                snap["state"],
                snap["size_mult"],
                snap["confidence"],
                msg,
            )
        else:
            _STATE["last_error"] = msg
            log.warning("push failed: %s", msg)
    else:
        log.info("no push (unchanged) state=%s", snap.get("state"))
    return snap


def main() -> None:
    cfg = load_config()
    logging.basicConfig(
        level=getattr(logging, cfg["log_level"], logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    log.info(
        "starting market oracle poll=%ss heartbeat=%ss dry_run=%s ingest=%s",
        cfg["poll_interval_sec"],
        cfg["heartbeat_sec"],
        cfg["dry_run"],
        bool(cfg["bot_ingest_url"]),
    )
    _start_health(cfg["port"])
    client = MarketDataClient()
    hyst = StateHysteresis(min_bars_to_flip=cfg["min_bars_to_flip"])
    backoff = cfg["poll_interval_sec"]
    while True:
        try:
            run_once(cfg, client, hyst)
            backoff = cfg["poll_interval_sec"]
        except Exception as e:
            _STATE["last_error"] = str(e)
            log.exception("poll failed: %s", e)
            backoff = min(3600, max(backoff * 2, cfg["poll_interval_sec"]))
        time.sleep(backoff)


if __name__ == "__main__":
    main()
