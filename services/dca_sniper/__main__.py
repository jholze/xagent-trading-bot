"""Standalone DCA Sniper Service (own Railway container).

  python -m services.dca_sniper

Env:
  PORT / DCA_SNIPER_PORT       health port (Railway injects PORT)
  DCA_SNIPER_ENABLED           1/0 (default 1 in this process)
  DCA_SNIPER_TOKEN             shared with bot internal APIs
  DCA_SNIPER_BOT_URL           bot base (https://xagent-test-....railway.app)
  REDIS_URL                    shared Redis (state, wake, price cache)
  DCA_SNIPER_POLL_SEC          poll interval
  RUN_DCA_SNIPER=1             railway_start selector
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Standalone process: force enabled unless explicitly disabled
if "DCA_SNIPER_ENABLED" not in os.environ:
    os.environ["DCA_SNIPER_ENABLED"] = "1"
# Never run bot in-process path from this container
os.environ["DCA_SNIPER_IN_PROCESS"] = "0"


def main() -> None:
    from flask import Flask, jsonify

    from logger import log
    from services.dca_sniper.config import (
        bot_base_url,
        dca_sniper_config,
        dca_sniper_enabled,
        internal_token,
    )
    from services.dca_sniper.loop import DcaSniperLoop
    from services.dca_sniper import state as sniper_state
    from services.dca_sniper.engine import run_cycle
    from services.dca_sniper.redis_bus import redis_available

    sniper_state.load_state()
    cfg = dca_sniper_config()
    loop = DcaSniperLoop()
    loop.start()

    app = Flask(__name__)

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify(
            {
                "ok": True,
                "service": "dca_sniper",
                "standalone": True,
                "enabled": dca_sniper_enabled(),
                "redis": redis_available(),
                "bot_url": bot_base_url(),
                "token_configured": bool(internal_token()),
                "focus": sniper_state.get_state().get("focus"),
            }
        ), 200

    @app.route("/status", methods=["GET"])
    def status():
        st = sniper_state.get_state()
        return jsonify(
            {
                "ok": True,
                "standalone": True,
                "enabled": dca_sniper_enabled(),
                "redis": redis_available(),
                "bot_url": bot_base_url(),
                "config": {
                    "max_focus_slots": cfg["max_focus_slots"],
                    "poll_interval_sec": cfg["poll_interval_sec"],
                    "ws_enabled": cfg["ws_enabled"],
                    "ws_move_pct": cfg.get("ws_move_pct"),
                    "require_reclaim_for_dca": cfg.get("require_reclaim_for_dca"),
                    "prefer_small_before_heavy": cfg.get("prefer_small_before_heavy"),
                    "in_process_tick": cfg.get("in_process_tick"),
                },
                "state": st,
                "last_audit": loop.last_audit(),
                "watch": sniper_state.focus_symbols(),
            }
        ), 200

    @app.route("/wake", methods=["POST", "GET"])
    def wake():
        from flask import request

        reason = "http_wake"
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            reason = str(data.get("reason") or reason)
        loop.request_wake(reason)
        return jsonify({"ok": True, "woken": True, "reason": reason}), 200

    @app.route("/cycle", methods=["POST"])
    def cycle_now():
        dry = str(os.environ.get("DCA_SNIPER_DRY_RUN") or "").lower() in (
            "1",
            "true",
            "yes",
        )
        audit = run_cycle(dry_run=dry)
        return jsonify({"ok": True, "audit": audit, "sharp": not dry, "standalone": True}), 200

    port = int(os.environ.get("DCA_SNIPER_PORT") or os.environ.get("PORT") or 5105)
    log(
        f"dca_sniper STANDALONE :{port} enabled={dca_sniper_enabled()} "
        f"redis={redis_available()} bot={bot_base_url()}",
        "INFO",
    )
    if not internal_token():
        log("dca_sniper WARN: no DCA_SNIPER_TOKEN / EXIT_WS_INTERNAL_TOKEN — bot calls will 503", "WARNING")
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
