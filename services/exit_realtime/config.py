"""Config for realtime exit path (Gate WS)."""

from __future__ import annotations

import os
from typing import Any


def exit_realtime_config(raw: dict | None = None) -> dict[str, Any]:
    if raw is None:
        try:
            from core.config import get_bot_config

            raw = get_bot_config().raw
        except Exception:
            raw = {}
    cfg = (raw or {}).get("exit_realtime")
    return dict(cfg) if isinstance(cfg, dict) else {}


def exit_realtime_enabled(raw: dict | None = None) -> bool:
    return bool(exit_realtime_config(raw).get("enabled"))


def exit_realtime_mode(raw: dict | None = None) -> str:
    mode = str(exit_realtime_config(raw).get("mode") or "shadow").strip().lower()
    if mode not in ("shadow", "live", "off"):
        return "shadow"
    return mode


def exit_realtime_sources(raw: dict | None = None) -> frozenset[str]:
    cfg = exit_realtime_config(raw)
    src = cfg.get("sources") or ["trailing_take_profit", "trailing_stop"]
    if not isinstance(src, (list, tuple)):
        src = ["trailing_take_profit", "trailing_stop"]
    return frozenset(str(s) for s in src)


def is_exit_radar_sidecar_process() -> bool:
    """True when this process is the dedicated exit-radar Railway service."""
    if os.environ.get("RUN_EXIT_RADAR") == "1":
        return True
    name = (os.environ.get("RAILWAY_SERVICE_NAME") or "").strip()
    return name in ("xagent-exit-radar", "exit-radar")


def exit_realtime_owner(raw: dict | None = None) -> str:
    """Who runs the Gate WS hub: ``bot`` (default) or ``sidecar``.

    Env ``EXIT_REALTIME_OWNER`` overrides config.
    Sidecar process always acts as owner for its own hub start.
    """
    env = (os.environ.get("EXIT_REALTIME_OWNER") or "").strip().lower()
    if env in ("bot", "sidecar"):
        return env
    cfg = exit_realtime_config(raw)
    owner = str(cfg.get("owner") or "bot").strip().lower()
    if owner not in ("bot", "sidecar"):
        return "bot"
    return owner


def exit_realtime_should_run_hub(raw: dict | None = None) -> bool:
    """Whether this process should start the Gate WS hub."""
    if not exit_realtime_enabled(raw):
        return False
    if exit_realtime_mode(raw) == "off":
        return False
    if is_exit_radar_sidecar_process():
        # Sidecar always runs hub when enabled (owner should be sidecar in prod).
        return True
    # Bot process: only when owner is bot (legacy in-process mode).
    return exit_realtime_owner(raw) == "bot"


def exit_execute_url() -> str:
    """Sidecar → bot fire endpoint. Empty = local execute (in-process)."""
    return (os.environ.get("EXIT_EXECUTE_URL") or "").strip()


def exit_ws_internal_token() -> str:
    return (os.environ.get("EXIT_WS_INTERNAL_TOKEN") or "").strip()
