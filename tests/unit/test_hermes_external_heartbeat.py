"""External Hermes must not zombie-stale; Redis health decides liveness."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))


class TestExternalHermesHeartbeat(unittest.TestCase):
    def test_heartbeat_tick_drops_local_hermes_when_external(self):
        from bus.heartbeats import heartbeat_registry
        import services.architecture_runtime as rt

        heartbeat_registry.clear()
        heartbeat_registry.beat("hermes", ttl_sec=1)

        cfg = MagicMock()
        cfg.architecture_config = {
            "hermes_external": True,
            "heartbeat_ttl_sec": 120,
            "key_prefix": "aria:",
            "notification_mode": "async",
        }
        cfg.hermes_enabled = True

        with patch.object(rt, "hermes_runs_in_process", return_value=False):
            rt._heartbeat_tick(cfg)

        self.assertNotIn("hermes", heartbeat_registry.all_workers())
        self.assertIn("monolith", heartbeat_registry.all_workers())

    def test_maybe_warn_skips_zombie_hermes_when_redis_ok(self):
        from bus.heartbeats import heartbeat_registry
        import services.architecture_runtime as rt

        heartbeat_registry.clear()
        # Zombie: expired local hermes (the old bug)
        heartbeat_registry.beat("hermes", ttl_sec=1)
        import time

        time.sleep(1.15)

        cfg = MagicMock()
        cfg.architecture_config = {
            "hermes_external": True,
            "heartbeat_ttl_sec": 1,
            "heartbeat_warn_enabled": True,
            "key_prefix": "aria:",
            "hermes_heartbeat_max_age_sec": 7200,
        }
        cfg.hermes_enabled = True
        rt._last_stale_warn_at = 0.0

        sent = []

        def capture(text, **kwargs):
            sent.append(text)
            return True

        with patch.object(rt, "hermes_runs_in_process", return_value=False), patch(
            "bus.heartbeats.HeartbeatRegistry.redis_alive", return_value=True
        ), patch("telegram_notifier.send_telegram_message", side_effect=capture):
            rt._maybe_warn_stale(cfg)

        self.assertEqual(sent, [])
        self.assertNotIn("hermes", heartbeat_registry.all_workers())

    def test_maybe_warn_when_redis_hermes_missing(self):
        from bus.heartbeats import heartbeat_registry
        import services.architecture_runtime as rt

        heartbeat_registry.clear()
        cfg = MagicMock()
        cfg.architecture_config = {
            "hermes_external": True,
            "heartbeat_ttl_sec": 120,
            "heartbeat_warn_enabled": True,
            "key_prefix": "aria:",
            "hermes_heartbeat_max_age_sec": 100,
        }
        cfg.hermes_enabled = True
        rt._last_stale_warn_at = 0.0
        sent = []

        def capture(text, **kwargs):
            sent.append(text)
            return True

        with patch.object(rt, "hermes_runs_in_process", return_value=False), patch(
            "bus.heartbeats.HeartbeatRegistry.redis_alive", return_value=False
        ), patch("telegram_notifier.send_telegram_message", side_effect=capture):
            rt._maybe_warn_stale(cfg)

        self.assertEqual(len(sent), 1)
        self.assertIn("hermes", sent[0])


if __name__ == "__main__":
    unittest.main()
