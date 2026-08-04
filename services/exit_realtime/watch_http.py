"""Watch-set API for gainer WS board when hub runs on exit-radar sidecar.

Bot (owner=sidecar): after gainer REST scan → POST symbols here.
Sidecar: applies hub.update_watch_set so Gate WS subscribes for identify.

Auth: same token as fire path (EXIT_WS_INTERNAL_TOKEN / X-Exit-Ws-Token).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from flask import Flask, jsonify, request

from logger import log


def exit_radar_base_url() -> str:
    """Public or private base URL of exit-radar service (no trailing slash)."""
    for key in ("EXIT_RADAR_URL", "EXIT_RADAR_BASE_URL"):
        raw = (os.environ.get(key) or "").strip().rstrip("/")
        if raw:
            return raw if raw.startswith("http") else f"https://{raw}"
    host = (os.environ.get("RAILWAY_SERVICE_XAGENT_EXIT_RADAR_URL") or "").strip()
    if host:
        return host if host.startswith("http") else f"https://{host}"
    return ""


def register_exit_ws_watch_routes(app: Flask) -> None:
    """POST /internal/exit-ws/watch-set — token-gated watch subscribe on local hub."""

    @app.route("/internal/exit-ws/watch-set", methods=["POST"])
    def internal_exit_ws_watch_set():
        from services.exit_realtime.config import exit_ws_internal_token
        from services.exit_realtime.hub import get_hub

        expected = exit_ws_internal_token()
        if not expected:
            return (
                jsonify(
                    {
                        "ok": False,
                        "message": "not_configured",
                        "error": "EXIT_WS_INTERNAL_TOKEN unset",
                    }
                ),
                503,
            )
        got = (
            request.headers.get("X-Exit-Ws-Token")
            or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            or ""
        )
        if got != expected:
            return jsonify({"ok": False, "message": "unauthorized"}), 401

        data: dict[str, Any] = request.get_json(silent=True) or {}
        symbols = data.get("symbols") or []
        if not isinstance(symbols, list):
            return jsonify({"ok": False, "message": "bad_args"}), 400
        clean = [str(s).strip() for s in symbols if str(s).strip()]

        hub = get_hub()
        if hub is None:
            return (
                jsonify(
                    {
                        "ok": False,
                        "message": "hub_not_running",
                        "applied": 0,
                    }
                ),
                503,
            )
        applied = hub.update_watch_set(clean)
        log(
            f"exit_ws watch-set API applied={len(applied)} "
            f"sample={','.join(applied[:5])}",
            "INFO",
        )
        return (
            jsonify(
                {
                    "ok": True,
                    "applied": len(applied),
                    "symbols": applied[:80],
                    "hub": hub.stats(),
                }
            ),
            200,
        )

    log("exit_ws watch-set route registered (/internal/exit-ws/watch-set)", "INFO")


def push_watch_set_remote(symbols: list[str], *, timeout_sec: float = 8.0) -> dict[str, Any]:
    """Bot → sidecar: seed watch set. Fail-open dict with ok=False on errors."""
    from services.exit_realtime.config import exit_ws_internal_token

    base = exit_radar_base_url()
    token = exit_ws_internal_token()
    if not base:
        return {"ok": False, "message": "no_radar_url"}
    if not token:
        return {"ok": False, "message": "no_token"}
    url = f"{base.rstrip('/')}/internal/exit-ws/watch-set"
    body = json.dumps({"symbols": list(symbols or [])}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Exit-Ws-Token": token,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                data = {"ok": False, "message": "bad_json", "raw": raw[:200]}
            if not isinstance(data, dict):
                return {"ok": False, "message": "bad_response"}
            data.setdefault("ok", resp.status == 200)
            return data
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            detail = str(e)
        return {"ok": False, "message": f"http_{e.code}", "detail": detail}
    except Exception as e:
        return {"ok": False, "message": str(e)[:160]}
