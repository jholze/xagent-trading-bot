"""#305 item 5: /diag command — cycle age, failures, stale workers, sources, queues."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from notifications.telegram_commands.diag_commands import build_diag_report, handle
from notifications.telegram_commands.router import dispatch_command


class TestDiagCommand(unittest.TestCase):
    def test_handle_diag(self):
        with patch("notifications.telegram_commands.diag_commands.send_telegram_message", return_value=True), \
             patch("notifications.telegram_commands.diag_commands.build_diag_report", return_value="x"):
            self.assertTrue(handle("/diag"))
            self.assertFalse(handle("/status"))
            self.assertFalse(handle("/help"))

    def test_dispatch_diag_not_unknown(self):
        with patch("notifications.telegram_commands.diag_commands.send_telegram_message") as send, \
             patch("notifications.telegram_commands.diag_commands.build_diag_report", return_value="🩺 diag"):
            self.assertTrue(dispatch_command("/diag"))
            send.assert_called()
            text = send.call_args[0][0]
            self.assertIn("diag", text.lower())

    def test_report_contains_sections(self):
        env = {
            "X_API_BEARER_TOKEN": "x",
            "CMC_API_KEY": "",
            "LUNARCRUSH_API_KEY": "lc",
        }
        with patch.dict(os.environ, env, clear=False), \
             patch("core.cycle_health.snapshot", return_value={
                 "last_cycle_age_sec": 12,
                 "consecutive_failures": 0,
             }), \
             patch("bus.heartbeats.heartbeat_registry.stale_workers", return_value=["eval_worker"]), \
             patch("bus.eval_queue.queue_depth", return_value=3), \
             patch("bus.trade_intents.trade_intent_queue.depth", return_value=1), \
             patch("data.lunarcrush_provider.list_tier_blocked", return_value=False), \
             patch("data.cmc_capabilities.cached_capabilities", return_value=None):
            text = build_diag_report()
        self.assertIn("12", text)
        self.assertIn("eval_worker", text)
        self.assertIn("X", text)
        self.assertIn("CMC", text)
        self.assertIn("LunarCrush", text)
        self.assertIn("1", text)
        self.assertIn("3", text)


if __name__ == "__main__":
    unittest.main()
