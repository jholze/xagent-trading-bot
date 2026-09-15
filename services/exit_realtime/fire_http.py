"""Bot-side HTTP API: accept trail-exit fires from the exit-radar sidecar."""

from __future__ import annotations

from typing import Any

from flask import Flask, jsonify, request

from logger import log


def register_exit_ws_fire_routes(app: Flask) -> None:
    """POST /internal/exit-ws/fire — token-gated execute on the bot."""

    @app.route("/internal/exit-ws/fire", methods=["POST"])
    def internal_exit_ws_fire():
        from services.exit_realtime.config import exit_ws_internal_token
        from services.exit_realtime.execute import (
            restoring_tenant_cycle_context,
            try_execute_trail_exit,
        )

        expected = exit_ws_internal_token()
        if not expected:
            return (
                jsonify(
                    {
                        "ok": False,
                        "executed": False,
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
            return jsonify({"ok": False, "executed": False, "message": "unauthorized"}), 401

        data: dict[str, Any] = request.get_json(silent=True) or {}
        symbol = str(data.get("symbol") or "")
        timeframe = str(data.get("timeframe") or "1h")
        try:
            price = float(data.get("price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        action = str(data.get("action") or "SELL_FULL")
        exit_source = str(data.get("exit_source") or "")
        rationale = str(data.get("rationale") or "")[:240]
        # Fail-closed: never silently default tenant (unlike timeframe above).
        tid = str(data.get("tenant_id") or "").strip()

        if not symbol or price <= 0:
            return (
                jsonify({"ok": False, "executed": False, "message": "bad_args"}),
                400,
            )

        if not tid:
            log(
                f"exit_ws fire API missing tenant_id symbol={symbol} "
                f"src={exit_source} — fail-closed",
                "WARNING",
            )
            return (
                jsonify(
                    {
                        "ok": False,
                        "executed": False,
                        "message": "missing_tenant_id",
                    }
                ),
                400,
            )

        from core.tenant_routing import iter_price_cycle_tenants

        known = {str(t).strip() for t in iter_price_cycle_tenants()}
        if tid not in known:
            log(
                f"exit_ws fire API unknown tenant_id={tid} symbol={symbol} "
                f"src={exit_source} — fail-closed",
                "WARNING",
            )
            return (
                jsonify(
                    {
                        "ok": False,
                        "executed": False,
                        "message": "unknown_tenant_id",
                    }
                ),
                400,
            )

        # Always local execute on the bot (ignore EXIT_EXECUTE_URL if set by mistake).
        # Restore _active_key after the switch — tenant_cycle_context does not.
        with restoring_tenant_cycle_context(tid):
            result = try_execute_trail_exit(
                symbol=symbol,
                timeframe=timeframe,
                price=price,
                action=action,
                exit_source=exit_source,
                rationale=rationale,
                force_local=True,
            )
        status = 200 if result.get("ok") else 409
        log(
            f"exit_ws fire API tenant={tid} {symbol} src={exit_source} "
            f"executed={result.get('executed')} msg={str(result.get('message') or '')[:80]}",
            "INFO",
        )
        return jsonify(result), status

    log("exit_ws fire route registered (/internal/exit-ws/fire)", "INFO")
