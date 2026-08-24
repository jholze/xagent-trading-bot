"""Bot-side HTTP API: token-gated MCP buy/sell/lock via TradingService."""

from __future__ import annotations

import os
from typing import Any

from flask import Flask, jsonify, request

from logger import log


def _expected_token() -> str:
    return (
        os.environ.get("MCP_BOT_TOKEN") or os.environ.get("EXIT_WS_INTERNAL_TOKEN") or ""
    ).strip()


def _got_token() -> str:
    return (
        request.headers.get("X-Exit-Ws-Token")
        or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        or ""
    )


def _as_float(val: Any, default: float = 0.0) -> float:
    try:
        if val is None or val == "":
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


def _result_body(result: Any) -> dict[str, Any]:
    executed = bool(getattr(result, "executed", False))
    return {
        "ok": executed,
        "executed": executed,
        "message": str(getattr(result, "message", "") or ""),
    }


def register_mcp_bot_routes(app: Flask) -> None:
    """POST /internal/mcp/execute — token-gated execute on the bot."""

    @app.route("/internal/mcp/execute", methods=["POST"])
    def mcp_execute():
        expected = _expected_token()
        if not expected:
            return jsonify({"ok": False, "error": "not_configured"}), 503
        if _got_token() != expected:
            return jsonify({"ok": False, "error": "unauthorized"}), 401

        data: dict[str, Any] = request.get_json(silent=True) or {}
        action = str(data.get("action") or "").strip().lower()
        tenant_id = str(data.get("tenant_id") or "").strip()
        symbol = str(data.get("symbol") or "").strip()
        if not action or not tenant_id or not symbol:
            return jsonify({"ok": False, "error": "bad_args"}), 400

        timeframe = str(data.get("timeframe") or "1h").strip() or "1h"
        actor_id = str(data.get("actor_id") or "").strip()
        reason = str(data.get("reason") or "").strip()
        price = _as_float(data.get("price"))
        usdt = _as_float(data.get("usdt"))
        source = f"mcp:{actor_id}" if actor_id else "mcp"

        from core.tenant_context import tenant_context

        with tenant_context(tenant_id):
            if action == "buy":
                from services.trading_service import TradingService

                result = TradingService().execute_buy(
                    symbol, timeframe, price=price, usdt=usdt
                )
                return jsonify(_result_body(result)), 200

            if action == "sell":
                amount = _sell_amount(data, symbol, timeframe)
                if amount is None:
                    return jsonify({"ok": False, "error": "amount_or_pct_required"}), 400
                if amount <= 0:
                    return jsonify(
                        {"ok": False, "executed": False, "message": "bad_amount"}
                    ), 400
                from services.trading_service import TradingService

                result = TradingService().execute_sell(
                    symbol, timeframe, price, signal=source, amount=amount
                )
                return jsonify(_result_body(result)), 200

            if action == "lock":
                from strategies.position_lock import DEFAULT_MODES, build_lock
                from strategies.positions import set_position_lock

                lock = build_lock(
                    reason=reason or "mcp_lock",
                    locked_by=source,
                    until=None,
                    modes=DEFAULT_MODES,
                )
                out = set_position_lock(symbol, timeframe, lock, persist=True)
                return jsonify(
                    {"ok": True, "executed": True, "message": "locked", "lock": out}
                ), 200

            if action == "unlock":
                from strategies.positions import set_position_lock

                set_position_lock(symbol, timeframe, None, persist=True)
                return jsonify({"ok": True, "executed": True, "message": "unlocked"}), 200

        return jsonify({"ok": False, "error": "bad_action"}), 400

    log("mcp execute route registered (/internal/mcp/execute)", "INFO")


def _sell_amount(data: dict[str, Any], symbol: str, timeframe: str) -> float | None:
    """Prefer explicit amount; else position.amount * pct/100 via get_position."""
    raw_amount = data.get("amount")
    raw_pct = data.get("pct")
    if raw_amount is not None and raw_amount != "":
        return _as_float(raw_amount)
    if raw_pct is not None and raw_pct != "":
        from strategies.positions import get_position

        pos = get_position(symbol, timeframe) or {}
        return _as_float(pos.get("amount")) * (_as_float(raw_pct) / 100.0)
    return None
