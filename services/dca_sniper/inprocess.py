"""In-process sniper tick — thin wrapper around run_cycle + LocalBotClient.

Prefer standalone service (in_process_tick=false). This path exists only as
fallback when explicitly enabled.
"""

from __future__ import annotations

import time
from typing import Any

from logger import log
from services.dca_sniper.config import dca_sniper_config, dca_sniper_enabled
from services.dca_sniper.engine import run_cycle
from services.dca_sniper.local_client import LocalBotClient

_last_tick = 0.0


def maybe_tick_dca_sniper(*, force: bool = False) -> dict[str, Any] | None:
    """Run one sniper cycle against local bot APIs (no network)."""
    global _last_tick
    cfg = dca_sniper_config()
    if not dca_sniper_enabled() and not force:
        return None
    if not cfg.get("in_process_tick") and not force:
        return None
    if cfg.get("notify_only"):
        log("dca_sniper in-process: notify_only=true → dry_run cycle", "WARNING")

    interval = float(cfg.get("poll_interval_sec") or 180)
    now = time.time()
    if not force and (now - _last_tick) < interval:
        return None
    _last_tick = now

    dry = bool(cfg.get("notify_only"))
    try:
        audit = run_cycle(client=LocalBotClient(), dry_run=dry)
        audit["mode"] = "in_process"
        return audit
    except Exception as e:
        log(f"dca_sniper in-process fail: {e}", "WARNING")
        return {"error": str(e)[:200], "mode": "in_process"}
