"""DCA sniper board health: Redis heartbeat, not leftover state."""

from __future__ import annotations

import os
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from services.exit_radar.sniper_status import fetch_dca_sniper_status


class TestSniperStatus(unittest.TestCase):
    def setUp(self):
        self._env = patch.dict(
            os.environ,
            {"DCA_SNIPER_URL": "", "DCA_SNIPER_PUBLIC_URL": ""},
            clear=False,
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def _redis_stack(self, client, state):
        empty_root = Path("/tmp/xagent-no-sniper-state")
        stack = ExitStack()
        stack.enter_context(
            patch("services.dca_sniper.redis_bus.redis_available", return_value=True)
        )
        stack.enter_context(
            patch("services.dca_sniper.redis_bus.load_state_redis", return_value=state)
        )
        stack.enter_context(
            patch("services.dca_sniper.redis_bus.key_prefix", return_value="test:")
        )
        stack.enter_context(patch("bus.redis_client.get_redis", return_value=client))
        stack.enter_context(
            patch("services.exit_radar.sniper_status._REPO_ROOT", empty_root)
        )
        return stack

    def test_redis_state_without_heartbeat_is_unhealthy(self):
        client = MagicMock()
        client.get.return_value = None
        state = {"focus": ["AAA/USDT"], "updated_at": "stale"}
        with self._redis_stack(client, state):
            out = fetch_dca_sniper_status()
        self.assertFalse(out["healthy"])
        self.assertEqual(out.get("error"), "redis_no_heartbeat")

    def test_redis_heartbeat_is_healthy(self):
        client = MagicMock()
        client.get.return_value = b"ok"
        state = {"focus": ["AAA/USDT"], "updated_at": "now"}
        with self._redis_stack(client, state):
            out = fetch_dca_sniper_status()
        self.assertTrue(out["healthy"])
        self.assertEqual(out["source"], "redis")
        self.assertTrue(out["ok"])
