"""Flask routes: /exit-radar GUI on the trading bot (Railway)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from flask import Response, jsonify, request, stream_with_context
from logger import log

ROOT = Path(__file__).resolve().parents[1]
GUI_PATH = ROOT / "tools" / "exit_radar" / "static" / "index.html"


def _token_ok() -> bool:
    """Optional gate: set EXIT_RADAR_TOKEN env; then require ?token= or header."""
    expected = (os.environ.get("EXIT_RADAR_TOKEN") or "").strip()
    if not expected:
        return True
    got = (
        request.args.get("token")
        or request.headers.get("X-Exit-Radar-Token")
        or ""
    ).strip()
    return got == expected


def _unauthorized():
    return jsonify({"ok": False, "error": "unauthorized"}), 401


def build_radar_snapshot() -> dict[str, Any]:
    """Snapshot compatible with exit radar GUI (from bot hub + pure eval)."""
    from services.exit_realtime.config import (
        exit_realtime_enabled,
        exit_realtime_mode,
    )
    from services.exit_realtime.hub import get_hub

    hub = get_hub()
    mode = exit_realtime_mode()
    enabled = exit_realtime_enabled()

    bot_hub: dict[str, Any]
    if not enabled:
        bot_hub = {"running": False, "disabled": True}
    elif hub is None:
        bot_hub = {"running": False, "disabled": False, "mode": mode}
    else:
        bot_hub = {"running": True, "disabled": False, "mode": mode, **hub.stats()}

    exits: list[dict[str, Any]] = []
    pairs_out: list[dict[str, Any]] = []
    connected = bool(bot_hub.get("connected"))
    started = time.time()

    try:
        from scripts.gate_ws_live_dashboard import evaluate_position
    except Exception:
        evaluate_position = None  # type: ignore

    # Prefer full position rows from dashboard loader (ttp/ts/life params)
    prices: dict[str, float] = {}
    if hub is not None:
        prices = hub.last_prices()
        # merge live peaks from hub book into price map awareness
        for row in hub.book_snapshot():
            sym = row["symbol"]
            if row.get("last_price"):
                prices.setdefault(sym, float(row["last_price"]))

    pos_rows: list[dict[str, Any]] = []
    try:
        from scripts.gate_ws_live_dashboard import load_open_positions
        from data_manager import resolve_ledger_scope

        scope = resolve_ledger_scope()
        pos_rows = load_open_positions(str(scope or "demo"))
    except Exception as exc:
        log(f"exit_radar load_open_positions: {exc}", "DEBUG")
        # fallback from hub book only
        if hub is not None:
            for row in hub.book_snapshot():
                pos = dict(row.get("position") or {})
                entry = float(pos.get("average_entry") or row.get("average_entry") or 0)
                pos_rows.append(
                    {
                        "symbol": row["symbol"],
                        "timeframe": row.get("timeframe") or "1h",
                        "entry": entry,
                        "amount": float(pos.get("amount") or 0),
                        "recent_high": float(
                            pos.get("recent_high") or row.get("recent_high") or 0
                        ),
                        "ttp": (row.get("strategy_params") or {}).get(
                            "trailing_take_profit"
                        )
                        or {"enabled": True, "arm_gain_pct": 12, "min_gain_pct": 10},
                        "trailing_stop": (row.get("strategy_params") or {}).get(
                            "trailing_stop"
                        )
                        or {
                            "enabled": True,
                            "activation_gain_pct": 5,
                            "min_trail_pct": 8,
                            "max_trail_pct": 25,
                            "atr_multiplier": 2,
                        },
                        "life": (row.get("strategy_params") or {}).get(
                            "profit_max_lifetime"
                        )
                        or {"enabled": True, "arm_gain_pct": 3, "max_hours": 96, "min_gain_pct": 1},
                        "stop_loss_pct": 50,
                        "partial_stop_pct": 25,
                        "prefer_full_close": True,
                    }
                )

    # overlay live recent_high from hub
    hub_peaks: dict[str, float] = {}
    if hub is not None:
        for row in hub.book_snapshot():
            hub_peaks[row["symbol"]] = float(row.get("recent_high") or 0)

    for pos in pos_rows:
        sym = pos["symbol"]
        price = float(prices.get(sym) or 0)
        if hub_peaks.get(sym):
            pos = dict(pos)
            pos["recent_high"] = max(float(pos.get("recent_high") or 0), hub_peaks[sym])
        atr = 5.0
        if evaluate_position and price > 0 and float(pos.get("entry") or 0) > 0:
            try:
                exits.append(evaluate_position(pos, price, atr_pct_est=atr))
            except Exception as exc:
                exits.append(
                    {
                        "ok": False,
                        "symbol": sym,
                        "price": price,
                        "entry": pos.get("entry"),
                        "error": str(exc)[:80],
                        "would_exit": False,
                        "near_exit": False,
                        "status": "error",
                    }
                )
        else:
            entry = float(pos.get("entry") or 0)
            amount = float(pos.get("amount") or 0)
            gain = ((price / entry) - 1) * 100 if entry > 0 and price > 0 else None
            exits.append(
                {
                    "ok": True,
                    "symbol": sym,
                    "timeframe": pos.get("timeframe"),
                    "price": price,
                    "entry": entry,
                    "recent_high": pos.get("recent_high"),
                    "gain_pct": gain,
                    "peak_gain_pct": None,
                    "drop_from_high_pct": 0,
                    "notional_usdt": amount * (price or entry),
                    "pnl_usdt": amount * (price - entry) if price and entry else 0,
                    "would_exit": False,
                    "near_exit": False,
                    "would_sources": [],
                    "near_sources": [],
                    "status": "waiting_tick" if price <= 0 else "idle",
                    "urgency": 0,
                    "ttp": pos.get("ttp") or {"enabled": True},
                    "trailing_stop": pos.get("trailing_stop") or {"enabled": True},
                    "stop_loss": {"pct": pos.get("stop_loss_pct") or 50},
                    "life": pos.get("life") or {"enabled": True},
                    "prefer_full_close": True,
                }
            )
        pairs_out.append(
            {
                "symbol": sym,
                "last": price,
                "updates": 0,
                "last_delta_pct": 0,
                "age_ms": None,
            }
        )

    n_would = sum(1 for e in exits if e.get("would_exit"))
    n_near = sum(1 for e in exits if e.get("near_exit") and not e.get("would_exit"))
    n_profit = sum(1 for e in exits if (e.get("gain_pct") is not None and e["gain_pct"] > 0))
    n_loss = sum(1 for e in exits if (e.get("gain_pct") is not None and e["gain_pct"] < 0))
    total_pnl = sum(float(e.get("pnl_usdt") or 0) for e in exits)
    total_notional = sum(float(e.get("notional_usdt") or 0) for e in exits)

    ticks = int(bot_hub.get("ticks") or 0)
    elapsed = max(0.001, time.time() - (started - 1))  # placeholder rate
    last_tick = float(bot_hub.get("last_tick_at") or 0)
    rate = 0.0
    if last_tick > 0 and ticks > 0:
        # rough: not accurate without start time
        rate = 0.0

    return {
        "type": "snapshot",
        "connected": connected,
        "subscribed": connected and int(bot_hub.get("symbols") or 0) > 0,
        "last_stage": "tick_in" if connected else "connect",
        "stages": {},
        "bot_hub": bot_hub,
        "stats": {
            "ticker_updates": ticks,
            "updates_per_sec": rate,
            "would_enqueue": 0,
            "would_exit_fires": int(bot_hub.get("fires") or 0),
            "errors": 0,
            "elapsed_sec": 0,
            "positions": len(exits),
        },
        "exit_summary": {
            "positions": len(exits),
            "would_exit": n_would,
            "near_exit": n_near,
            "armed": 0,
            "in_profit": n_profit,
            "in_loss": n_loss,
            "total_pnl_usdt": round(total_pnl, 2),
            "total_notional_usdt": round(total_notional, 2),
        },
        "thresholds": {"min_delta_pct": 0.05, "enqueue_threshold_pct": 0.12},
        "pairs": pairs_out,
        "exits": exits,
        "source": "bot_exit_realtime",
    }


def register_exit_radar_routes(app) -> None:
    """Attach /exit-radar* routes to the main Flask app."""

    @app.route("/exit-radar", methods=["GET"])
    @app.route("/exit-radar/", methods=["GET"])
    def exit_radar_index():
        if not _token_ok():
            return _unauthorized()
        if not GUI_PATH.is_file():
            return "exit radar GUI missing", 404
        html = GUI_PATH.read_text(encoding="utf-8")
        # inject base path for nested routes
        if "<head>" in html and 'id="exitRadarBase"' not in html:
            html = html.replace(
                "<head>",
                '<head>\n<script>window.EXIT_RADAR_BASE="/exit-radar";</script>',
                1,
            )
        return Response(html, mimetype="text/html; charset=utf-8")

    @app.route("/exit-radar/api/snapshot", methods=["GET"])
    @app.route("/api/snapshot", methods=["GET"])
    def exit_radar_snapshot():
        # /api/snapshot only when Referer is exit-radar — avoid clobbering other uses
        if request.path == "/api/snapshot":
            ref = request.headers.get("Referer") or ""
            if "/exit-radar" not in ref and request.args.get("radar") != "1":
                return jsonify({"ok": False, "error": "use /exit-radar/api/snapshot"}), 404
        if not _token_ok():
            return _unauthorized()
        try:
            return jsonify(build_radar_snapshot())
        except Exception as exc:
            log(f"exit_radar snapshot: {exc}", "WARNING")
            return jsonify({"ok": False, "error": str(exc)[:200]}), 500

    @app.route("/exit-radar/api/health", methods=["GET"])
    def exit_radar_health():
        if not _token_ok():
            return _unauthorized()
        from services.exit_realtime.hub import get_hub

        h = get_hub()
        return jsonify(
            {
                "ok": True,
                "hub": None if h is None else h.stats(),
                "gui": str(GUI_PATH.relative_to(ROOT)),
            }
        )

    @app.route("/exit-radar/events", methods=["GET"])
    @app.route("/events", methods=["GET"])
    def exit_radar_events():
        if request.path == "/events":
            ref = request.headers.get("Referer") or ""
            if "/exit-radar" not in ref and request.args.get("radar") != "1":
                return jsonify({"ok": False, "error": "use /exit-radar/events"}), 404
        if not _token_ok():
            return _unauthorized()

        from services.exit_realtime.hub import get_hub

        hub = get_hub()

        @stream_with_context
        def gen():
            # initial snapshot
            try:
                snap = build_radar_snapshot()
                yield f"data: {json.dumps(snap)}\n\n"
            except Exception as exc:
                yield f"data: {json.dumps({'type': 'error', 'msg': str(exc)[:120]})}\n\n"

            cq = None
            if hub is not None:
                cq = hub.subscribe_gui()
            try:
                while True:
                    if cq is not None:
                        try:
                            ev = cq.get(timeout=12.0)
                            yield f"data: {json.dumps(ev)}\n\n"
                            continue
                        except queue.Empty:
                            pass
                    else:
                        time.sleep(12)
                    # heartbeat + soft refresh
                    try:
                        yield f"data: {json.dumps(build_radar_snapshot())}\n\n"
                    except Exception:
                        yield ": ping\n\n"
            finally:
                if hub is not None and cq is not None:
                    hub.unsubscribe_gui(cq)

        # queue imported for Empty
        import queue

        return Response(
            gen(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    log("exit_radar HTTP routes registered (/exit-radar)", "INFO")
