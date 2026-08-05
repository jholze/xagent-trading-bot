"""Entrypoint: Gainer Signal Service (WS-1).

  python -m services.gainer_signal

Env:
  PORT                         Flask port (default 5101)
  GAINER_SIGNAL_TOKEN          shared with bot (or EXIT_WS_INTERNAL_TOKEN)
  GAINER_SIGNAL_BOT_URL        bot base or full .../internal/gainer-signal
  GAINER_SIGNAL_PUSH           1/0 enable push (default 1)
  GAINER_RECOGNIZE_TOP_N       default 100
  GAINER_ELIGIBLE_MIN_VOL      default 500000
  GAINER_REST_SEED_SEC         default 60
  GAINER_WS_MAX_SUBS           default 120
"""

from __future__ import annotations

import os
import sys
import time

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def main() -> None:
    from flask import Flask, jsonify, request

    from logger import log
    from services.gainer_signal.board import get_board, reset_board
    from services.gainer_signal.pure import DEFAULT_ELIGIBLE_MIN_VOL, DEFAULT_RECOGNIZE_TOP_N
    from services.gainer_signal.ws_loop import GainerWsRuntime

    top_n = int(os.environ.get("GAINER_RECOGNIZE_TOP_N") or DEFAULT_RECOGNIZE_TOP_N)
    min_vol = float(os.environ.get("GAINER_ELIGIBLE_MIN_VOL") or DEFAULT_ELIGIBLE_MIN_VOL)
    rest_sec = float(os.environ.get("GAINER_REST_SEED_SEC") or 60)
    ws_max = int(os.environ.get("GAINER_WS_MAX_SUBS") or 120)
    push = str(os.environ.get("GAINER_SIGNAL_PUSH") or "1").strip() not in (
        "0",
        "false",
        "no",
        "off",
    )

    board = reset_board()
    runtime = GainerWsRuntime(
        board,
        top_n=top_n,
        min_vol=min_vol,
        rest_seed_sec=rest_sec,
        ws_max_subscriptions=ws_max,
        push_enabled=push,
    )
    # initial seed before serving
    try:
        runtime.seed_once()
    except Exception as e:
        log(f"gainer_signal initial seed failed: {e}", "WARNING")
    runtime.start()

    app = Flask(__name__)

    @app.route("/health", methods=["GET"])
    def health():
        st = board.stats()
        return (
            jsonify(
                {
                    "status": "OK",
                    "service": "xagent-gainer-signal",
                    "connected": st.get("connected"),
                    "n_subscribed": st.get("n_subscribed"),
                    "n_recognized": st.get("n_recognized"),
                    "n_eligible": st.get("n_eligible"),
                    "ticks": st.get("ticks"),
                    "rest_seeds": st.get("rest_seeds"),
                    "signals_emitted": st.get("signals_emitted"),
                    "signals_pushed_ok": st.get("signals_pushed_ok"),
                    "signals_push_fail": st.get("signals_push_fail"),
                    "last_board_at": st.get("last_board_at"),
                    "reconnects": st.get("reconnects"),
                    "top_n": top_n,
                    "eligible_min_vol": min_vol,
                    "push_enabled": push,
                }
            ),
            200,
        )

    @app.route("/leaders", methods=["GET"])
    def leaders():
        eligible_only = str(request.args.get("eligible") or "").lower() in (
            "1",
            "true",
            "yes",
        )
        limit = request.args.get("limit")
        rows = board.leaders(eligible_only=eligible_only)
        if limit:
            try:
                rows = rows[: max(1, int(limit))]
            except ValueError:
                pass
        return (
            jsonify(
                {
                    "ok": True,
                    "n": len(rows),
                    "leaders": rows,
                    "stats": board.stats(),
                    "ts": time.time(),
                }
            ),
            200,
        )

    @app.route("/signals/preview", methods=["GET"])
    def signals_preview():
        sigs = board.select_signals()
        return jsonify({"ok": True, "n": len(sigs), "signals": sigs}), 200

    port = int(os.environ.get("PORT") or os.environ.get("GAINER_SIGNAL_PORT") or "5101")
    log(
        f"=== gainer-signal service listen 0.0.0.0:{port} top_n={top_n} "
        f"min_vol={min_vol} push={push} ===",
        "INFO",
    )
    app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
