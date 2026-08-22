"""Read-only desk HTTP: token-gated snapshot/ohlcv + /desk SPA."""

from __future__ import annotations

import os
from pathlib import Path

from flask import Flask, jsonify, request, send_file

from logger import log

_ROOT = Path(__file__).resolve().parents[1]
_DIST = _ROOT / "tools" / "desk" / "dist"
_MISSING_BUILD = "desk UI missing — run npm run build in tools/desk"


def _require_token():
    expected = (
        os.environ.get("DESK_TOKEN") or os.environ.get("EXIT_WS_INTERNAL_TOKEN") or ""
    ).strip()
    if not expected:
        return jsonify({"ok": False, "error": "not_configured"}), 503
    got = (
        request.headers.get("X-Exit-Ws-Token")
        or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        or request.args.get("token")
        or ""
    ).strip()
    if got != expected:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    return None


def _safe_dist_file(rel: str) -> Path | None:
    if not rel or rel.endswith("/"):
        return None
    dist = Path(_DIST)
    try:
        dist_r = dist.resolve()
        target = (dist / rel).resolve()
        target.relative_to(dist_r)
    except (OSError, ValueError):
        return None
    if not target.is_file():
        return None
    return target


def _missing_spa():
    return _MISSING_BUILD, 404, {"Content-Type": "text/plain; charset=utf-8"}


def register_desk_routes(app: Flask) -> None:
    """Attach GET /internal/desk/* and /desk SPA (no POST)."""

    @app.route("/internal/desk/snapshot", methods=["GET"])
    def desk_snapshot():
        denied = _require_token()
        if denied is not None:
            return denied
        symbol = (request.args.get("symbol") or "").strip()
        if not symbol:
            return jsonify({"ok": False, "error": "symbol_required"}), 400
        tenant_id = (request.args.get("tenant") or "default").strip() or "default"
        tf = (request.args.get("tf") or "1h").strip() or "1h"
        try:
            from services.desk.ohlcv import load_ohlcv
            from services.desk.snapshot import build_snapshot

            facts = None
            try:
                pack = load_ohlcv(symbol, tf)
            except Exception as exc:
                log(f"desk snapshot ohlcv: {exc}", "WARNING")
                pack = {"ok": False}
            if isinstance(pack, dict) and pack.get("ok"):
                facts = {
                    "rsi": pack.get("last_rsi"),
                    "at_lower_bb": pack.get("at_lower_bb"),
                }
            snap = build_snapshot(tenant_id=tenant_id, symbol=symbol, facts=facts)
            return jsonify(snap), 200
        except Exception as exc:
            log(f"desk snapshot failed: {exc}", "WARNING")
            return jsonify({"ok": False, "error": "snapshot_failed"}), 200

    @app.route("/internal/desk/ohlcv", methods=["GET"])
    def desk_ohlcv():
        denied = _require_token()
        if denied is not None:
            return denied
        symbol = (request.args.get("symbol") or "").strip()
        tf = (request.args.get("tf") or "1h").strip() or "1h"
        try:
            from services.desk.ohlcv import load_ohlcv

            if not symbol:
                return jsonify({"ok": False, "error": "ohlcv_unavailable", "bars": []}), 200
            pack = load_ohlcv(symbol, tf)
            return jsonify(pack), 200
        except Exception as exc:
            log(f"desk ohlcv failed: {exc}", "WARNING")
            return jsonify({"ok": False, "error": "ohlcv_unavailable", "bars": []}), 200

    @app.route("/desk", methods=["GET"], strict_slashes=False)
    @app.route("/desk/<path:path>", methods=["GET"], strict_slashes=False)
    def desk_spa(path: str = ""):
        rel = (path or "").strip()
        if not rel or rel in ("/", "index.html"):
            index = Path(_DIST) / "index.html"
            if not index.is_file():
                return _missing_spa()
            return send_file(index, mimetype="text/html; charset=utf-8")

        target = _safe_dist_file(rel)
        if target is None:
            return "not found", 404
        return send_file(target)

    log("desk HTTP routes registered (/internal/desk, /desk)", "INFO")
