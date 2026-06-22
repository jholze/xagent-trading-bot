#!/usr/bin/env python3
"""Standalone notification worker (optional; monolith uses in-process thread)."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    from core.config import get_bot_config
    from services.architecture_runtime import ensure_started
    from bus.notifications import notification_publisher

    cfg = get_bot_config()
    ensure_started(force_refresh=True)
    print(f"Notification worker running (mode={cfg.architecture_config.get('notification_mode')})")
    try:
        while notification_publisher.running:
            time.sleep(5)
    except KeyboardInterrupt:
        notification_publisher.stop()


if __name__ == "__main__":
    main()