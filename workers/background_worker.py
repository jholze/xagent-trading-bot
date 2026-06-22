#!/usr/bin/env python3
"""Background worker stub (Phase 4) — heavy jobs + optional Redis consumer."""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.config import get_bot_config
from logger import log
from services.architecture_runtime import ensure_started


def main():
    cfg = get_bot_config()
    ensure_started(force_refresh=True)
    log("Background worker idle (in-process queue active in monolith)", "INFO")
    while True:
        try:
            from bus.heartbeats import heartbeat_registry

            arch = cfg.architecture_config
            heartbeat_registry.beat(
                "background_worker",
                ttl_sec=int(arch.get("heartbeat_ttl_sec", 120)),
                key_prefix=arch.get("key_prefix", "aria:"),
            )
        except Exception:
            pass
        time.sleep(30)


if __name__ == "__main__":
    main()