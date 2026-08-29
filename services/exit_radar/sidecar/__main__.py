"""Entrypoint: Gate WS exit hub + Exit Radar GUI (no trading bot cycles).

  RUN_EXIT_RADAR=1 python -m services.exit_radar.sidecar

Env:
  MONGO_URL / DEMO_*          ledger (same as bot)
  EXIT_EXECUTE_URL            bot fire URL, e.g. https://bot/internal/exit-ws/fire
  EXIT_WS_INTERNAL_TOKEN      shared secret with bot
  EXIT_REALTIME_OWNER=sidecar recommended
  PORT                        Flask port (Railway)
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _bootstrap_env() -> None:
    os.environ.setdefault("RUN_EXIT_RADAR", "1")
    os.environ.setdefault("DEMO_MODE", "1")
    os.environ.setdefault("DEMO_LEDGER_BACKEND", "mongo")
    os.environ.setdefault("MONGODB_DB", os.environ.get("MONGODB_DB") or "xagent_test")
    os.environ.setdefault("DEMO_ALLOW_REMOTE_MONGO", "1")
    os.environ.setdefault("EXIT_REALTIME_OWNER", "sidecar")
    # Order ledger flags common on staging bot
    os.environ.setdefault("ORDER_LEDGER_V2", "1")
    os.environ.setdefault("ORDER_LEDGER_V2_READS", "1")


def _sync_ledger() -> None:
    from data_manager import resolve_ledger_scope
    from logger import log
    from services.ledger_sync import rebuild_positions_from_orders, sync_positions_on_startup
    from storage.mongo_client import ping_database, resolve_database_name

    if not ping_database():
        raise SystemExit("exit-radar sidecar: Mongo ping failed")
    log(f"exit-radar sidecar mongo db={resolve_database_name()}", "INFO")
    scope = resolve_ledger_scope() or "demo"
    try:
        rebuild_positions_from_orders(scope)
        sync_positions_on_startup()
    except Exception as exc:
        log(f"exit-radar initial position sync: {exc}", "WARNING")


def _start_hub() -> None:
    from logger import log
    from services.exit_realtime.config import (
        exit_execute_url,
        exit_realtime_enabled,
        exit_realtime_mode,
    )
    from services.exit_realtime.hub import ensure_started, get_hub

    if not exit_execute_url():
        log(
            "exit-radar sidecar: EXIT_EXECUTE_URL unset — "
            "live fires will not reach the bot (set for production)",
            "WARNING",
        )
    if not exit_realtime_enabled():
        log("exit-radar sidecar: exit_realtime.enabled=false — hub idle", "WARNING")
        return
    hub = ensure_started()
    if hub is None:
        log(
            f"exit-radar sidecar: hub not started "
            f"(mode={exit_realtime_mode()})",
            "WARNING",
        )
        return
    st = hub.stats() if get_hub() else {}
    log(
        f"exit-radar sidecar hub up mode={exit_realtime_mode()} "
        f"symbols={st.get('symbols')} execute_url="
        f"{'set' if exit_execute_url() else 'unset'}",
        "INFO",
    )


def _start_watch_self_seed() -> None:
    """Background: REST live_top → hub watch when ws_board enabled (sidecar).

    Complements bot POST /internal/exit-ws/watch-set so identify works even if
    the bot cannot reach the radar URL yet.
    """
    import threading
    import time

    from logger import log

    def _loop() -> None:
        from services.exit_realtime.hub import get_hub
        from services.gainer_universe.config import gainer_universe_config
        from services.gainer_universe.scanner import (
            fetch_gate_tickers,
            filter_and_rank_live,
        )
        from services.gainer_universe.ws_board import (
            watch_symbols_from_gainer_state,
            ws_board_enabled,
        )

        while True:
            try:
                if not ws_board_enabled():
                    time.sleep(60)
                    continue
                cfg = gainer_universe_config()
                poll = max(30.0, float(cfg.get("poll_sec") or 60))
                hub = get_hub()
                if hub is None:
                    time.sleep(poll)
                    continue
                tickers = fetch_gate_tickers() or {}
                live_top = filter_and_rank_live(tickers, cfg) if tickers else []
                syms = watch_symbols_from_gainer_state(
                    {"live_top": live_top, "eligible": []},
                    {"gainer_universe": {**cfg}},
                )
                if syms:
                    hub.update_watch_set(syms)
                    log(
                        f"exit-radar self-seed watch n={len(syms)} "
                        f"top={syms[0] if syms else '-'}",
                        "INFO",
                    )
                time.sleep(poll)
            except Exception as exc:
                log(f"exit-radar watch self-seed: {exc}", "DEBUG")
                time.sleep(60)

    threading.Thread(
        target=_loop, name="exit-radar-watch-seed", daemon=True
    ).start()
    log("exit-radar watch self-seed thread started", "INFO")


def main() -> None:
    _bootstrap_env()

    from flask import Flask
    from logger import log

    log("=== exit-radar sidecar start ===", "INFO")
    _sync_ledger()
    _start_hub()

    app = Flask(__name__)

    @app.route("/health", methods=["GET"])
    def health():
        from services.exit_realtime.config import (
            exit_execute_url,
            exit_realtime_mode,
            exit_realtime_owner,
        )
        from services.exit_realtime.hub import get_hub

        hub = get_hub()
        stats = hub.stats() if hub else {"running": False}
        return {
            "status": "OK",
            "service": "xagent-exit-radar",
            "owner": exit_realtime_owner(),
            "mode": exit_realtime_mode(),
            "execute_url_set": bool(exit_execute_url()),
            "hub": stats,
        }, 200

    try:
        from services.exit_radar.http import register_exit_radar_routes

        register_exit_radar_routes(app)
    except Exception as exc:
        log(f"exit-radar routes failed: {exc}", "ERROR")
        raise

    try:
        from services.exit_realtime.watch_http import register_exit_ws_watch_routes

        register_exit_ws_watch_routes(app)
    except Exception as exc:
        log(f"exit-ws watch-set route failed: {exc}", "WARNING")

    # Optional self-seed if bot push is late (REST live_top only, light)
    try:
        _start_watch_self_seed()
    except Exception as exc:
        log(f"exit-radar watch self-seed failed: {exc}", "DEBUG")

    port = int(os.environ.get("PORT") or os.environ.get("EXIT_RADAR_PORT") or "5000")
    log(f"exit-radar sidecar listening on 0.0.0.0:{port}", "INFO")
    # threaded=True: SSE + snapshot + health concurrent
    app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
