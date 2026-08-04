"""Cycle-safe refresh of gainer universe state."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

from logger import log
from services.gainer_universe.config import gainer_universe_config, gainer_universe_enabled
from services.gainer_universe.scanner import run_scan
from services.gainer_universe.store import load_gainer_state, save_gainer_state

_lock = threading.Lock()
_last_live_mono: float = 0.0
_process_cache: dict[str, Any] = {}


def _push_ws_watch(state: dict, config: dict | None = None) -> None:
    """Seed WS board watch set: local hub (owner=bot) or remote sidecar.

    Staging uses EXIT_REALTIME_OWNER=sidecar — local get_hub() is None on the
    bot, so we POST /internal/exit-ws/watch-set to the exit-radar service.
    """
    try:
        from services.gainer_universe.ws_board import (
            watch_symbols_from_gainer_state,
            ws_board_enabled,
        )

        if not ws_board_enabled(config):
            return
        syms = watch_symbols_from_gainer_state(state, config)
        if not syms:
            return

        from services.exit_realtime.hub import get_hub

        hub = get_hub()
        if hub is not None:
            hub.update_watch_set(syms)
            log(f"gainer_ws_board watch seeded local n={len(syms)}", "INFO")
            return

        from services.exit_realtime.watch_http import push_watch_set_remote

        result = push_watch_set_remote(syms)
        if result.get("ok"):
            log(
                f"gainer_ws_board watch seeded remote n={result.get('applied', len(syms))}",
                "INFO",
            )
        else:
            log(
                f"gainer_ws_board remote seed skip: {result.get('message')}",
                "DEBUG",
            )
    except Exception as e:
        log(f"gainer_ws_board seed skip: {e}", "DEBUG")


def _refresh_sync(cfg: dict, root_config: dict | None = None) -> dict:
    """Blocking scan (call under lock or from worker)."""
    global _last_live_mono, _process_cache
    prev = load_gainer_state()
    try:
        snap = run_scan(cfg, prev_state=prev)
        state = {**prev, **snap}
        save_gainer_state(state)
        _process_cache = state
        _last_live_mono = time.monotonic()
        counts = snap.get("counts") or {}
        log(
            f"gainer_universe refresh mode={cfg.get('mode')} "
            f"live={counts.get('live_top', 0)} eligible={counts.get('eligible', 0)} "
            f"streaks={counts.get('streaks', 0)} "
            f"err={snap.get('last_error') or '-'}",
            "INFO",
        )
        _push_ws_watch(state, root_config if isinstance(root_config, dict) else None)
        return state
    except Exception as e:
        log(f"gainer_universe refresh failed (fail-open): {e}", "WARNING")
        return prev or {}


def maybe_refresh_gainer_universe(config: dict | None = None) -> dict:
    """Refresh live/daily boards if due. Never raises. Fail-open to last state.

    First cold start (no state / daily due): run in a background thread so the
    trading cycle and Railway /health are not blocked for minutes of OHLCV.
    """
    global _last_live_mono, _process_cache

    if not gainer_universe_enabled(config):
        return {}
    cfg = gainer_universe_config(config)
    poll = float(cfg.get("poll_sec") or 60)

    root = config if isinstance(config, dict) else None

    with _lock:
        now_m = time.monotonic()
        if _last_live_mono and (now_m - _last_live_mono) < poll and _process_cache:
            return dict(_process_cache)

        prev = load_gainer_state()
        has_eligible = bool(prev.get("eligible") or prev.get("live_top"))
        # Heavy path: no prior state → background so healthcheck can pass
        if not has_eligible and not _process_cache:
            def _bg():
                try:
                    with _lock:
                        _refresh_sync(cfg, root)
                except Exception as e:
                    log(f"gainer_universe bg refresh failed: {e}", "WARNING")

            threading.Thread(target=_bg, name="gainer-universe-bg", daemon=True).start()
            log("gainer_universe cold start: daily/live scan deferred to background", "INFO")
            return prev or {}

        return _refresh_sync(cfg, root)


def reset_gainer_runtime_cache() -> None:
    """Test helper."""
    global _last_live_mono, _process_cache
    with _lock:
        _last_live_mono = 0.0
        _process_cache = {}
