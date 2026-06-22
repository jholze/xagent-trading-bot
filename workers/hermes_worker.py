#!/usr/bin/env python3
"""Standalone Hermes agent (Phase 1) — use when architecture.hermes_external=true."""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.config import get_bot_config
from hermes.agent import HermesAgent
from logger import log


def main():
    cfg = get_bot_config()
    arch = cfg.architecture_config
    if not arch.get("hermes_external"):
        print("hermes_external=false — Hermes läuft im Monolithen. Abbruch.")
        sys.exit(1)
    if not cfg.hermes_enabled:
        print("hermes.enabled=false — nichts zu tun.")
        sys.exit(0)

    interval = int(cfg.hermes_config.get("cycle_interval_sec", 3600))
    agent = HermesAgent(cfg)
    log(f"Hermes external worker started (interval={interval}s)", "INFO")

    while True:
        try:
            cfg.refresh()
            result = agent.run_cycle()
            log(result.summary, "INFO")
            try:
                from bus.heartbeats import heartbeat_registry

                heartbeat_registry.beat(
                    "hermes",
                    ttl_sec=int(arch.get("heartbeat_ttl_sec", 120)),
                    key_prefix=arch.get("key_prefix", "aria:"),
                )
            except Exception:
                pass
        except Exception as e:
            log(f"Hermes worker error: {e}", "ERROR")
        time.sleep(interval)


if __name__ == "__main__":
    main()