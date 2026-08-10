#!/usr/bin/env python3
"""Standalone Hermes agent (Phase 1) — use when architecture.hermes_external=true."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.config import get_bot_config
from hermes.agent import HermesAgent
from logger import log


def main():
    """Delegate to intelligence.memory.service (memory + optional Hermes learning)."""
    cfg = get_bot_config()
    arch = cfg.architecture_config
    # Allow RUN_HERMES=1 even if hermes_external not yet flipped in config
    if not arch.get("hermes_external") and os.environ.get("RUN_HERMES") != "1":
        print("hermes_external=false — Hermes läuft im Monolithen. Abbruch.")
        sys.exit(1)
    from intelligence.memory.service import main as memory_main

    memory_main()


if __name__ == "__main__":
    main()