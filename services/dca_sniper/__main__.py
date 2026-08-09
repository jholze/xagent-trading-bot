"""Entrypoint: DCA Sniper Service.

  python -m services.dca_sniper

Env:
  PORT / DCA_SNIPER_PORT       health port (default 5105)
  DCA_SNIPER_ENABLED           1/0
  DCA_SNIPER_TOKEN             shared with bot
  DCA_SNIPER_BOT_URL           bot base URL
  DCA_SNIPER_POLL_SEC          override poll interval
  DCA_SNIPER_STATE_PATH        optional state file
"""

from __future__ import annotations

import os
import sys
import time

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def main() -> None:
    from flask import Flask, jsonify

    from logger import log
    from services.dca_sniper.config import dca_sniper_config, dca_sniper_enabled
    from services.dca_sniper.loop import DcaSniperLoop
    from services.dca_sniper import state as sniper_state
    from services.dca_sniper.engine import run_cycle

    sniper_state.load_state()
    cfg = dca_sniper_config()
    poll = os.environ.get("DCA_SNIPER_POLL_SEC")
    if poll:
        # monkey via env already handled if we pass config override
        pass

    loop = DcaSniperLoop()
    loop.start()

    app = Flask(__name__)

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify(
            {
                "ok": True,
                "service": "dca_sniper",
                "enabled": dca_sniper_enabled(),
                "focus": sniper_state.get_state().get("focus"),
            }
        ), 200

    @app.route("/status", methods=["GET"])
    def status():
        st = sniper_state.get_state()
        return jsonify(
            {
                "ok": True,
                "enabled": dca_sniper_enabled(),
                "config": {
                    "max_focus_slots": cfg["max_focus_slots"],
                    "poll_interval_sec": cfg["poll_interval_sec"],
                    "ws_enabled": cfg["ws_enabled"],
                },
                "state": st,
                "last_audit": loop.last_audit(),
            }
        ), 200

    @app.route("/wake", methods=["POST"])
    def wake():
        """WS / external wake — runs soon on loop."""
        loop.request_wake("http_wake")
        return jsonify({"ok": True, "woken": True}), 200

    @app.route("/cycle", methods=["POST"])
    def cycle_now():
        """Force one cycle — live execute when enabled (staging sharp)."""
        # only dry if explicitly requested; notify_only is config-driven inside run_cycle
        dry = str(os.environ.get("DCA_SNIPER_DRY_RUN") or "").lower() in (
            "1",
            "true",
            "yes",
        )
        audit = run_cycle(dry_run=dry)
        return jsonify({"ok": True, "audit": audit, "sharp": not dry}), 200

    port = int(os.environ.get("DCA_SNIPER_PORT") or os.environ.get("PORT") or 5105)
    log(
        f"dca_sniper service listening :{port} enabled={dca_sniper_enabled()}",
        "INFO",
    )
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
