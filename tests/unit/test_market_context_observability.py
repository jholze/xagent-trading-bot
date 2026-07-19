"""Market context impact observability."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from services.market_context_observability import (
    cycle_counters,
    format_fusion_line,
    maybe_notify_state_change,
    note_buy_blocked,
    note_size_cut,
    reset_cycle_counters,
)


class TestMarketContextObs(unittest.TestCase):
    def setUp(self):
        reset_cycle_counters()
        import services.market_context_observability as m

        m._last_notified = None
        m._last_notify_ts = 0.0

    def test_counters(self):
        note_buy_blocked(regime="CRASH", source="oracle", rationale="test")
        note_size_cut(mult=0.35, regime="RISK_OFF")
        note_size_cut(mult=0.35, regime="RISK_OFF")
        c = cycle_counters()
        self.assertEqual(c["buy_blocks"], 1)
        self.assertEqual(c["size_cuts"], 2)

    def test_fusion_line(self):
        line = format_fusion_line(
            {
                "active": True,
                "regime": "RISK_OFF",
                "size_mult": 0.35,
                "sensor_policy": "shadow",
                "source": "oracle",
                "block_buys": False,
            }
        )
        self.assertIn("RISK_OFF", line)
        self.assertIn("0.35", line)

    def test_state_change_notify_once(self):
        bias = {
            "active": True,
            "regime": "NEUTRAL",
            "size_mult": 0.85,
            "sensor_policy": "active",
            "source": "oracle",
            "block_buys": False,
            "rationale": "test",
        }
        with patch(
            "services.market_context_observability.state_change_notify_enabled",
            return_value=True,
        ), patch(
            "services.market_context_observability.min_notify_interval_sec",
            return_value=0,
        ), patch("telegram_notifier.send_telegram_message", return_value=True) as send:
            self.assertTrue(maybe_notify_state_change(bias))
            self.assertFalse(maybe_notify_state_change(bias))  # same
            bias2 = dict(bias, regime="RISK_OFF", size_mult=0.35)
            self.assertTrue(maybe_notify_state_change(bias2))
            self.assertGreaterEqual(send.call_count, 2)

    def test_jsonl_rotate_when_over_max(self):
        import tempfile
        from pathlib import Path

        from services.observability_store import append_jsonl, maybe_rotate_jsonl

        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "events.jsonl")
            for i in range(50):
                append_jsonl(path, {"i": i, "pad": "x" * 200})
            self.assertTrue(os.path.isfile(path))
            # force rotate with tiny budget
            ok = maybe_rotate_jsonl(path, max_bytes=500, keep_lines=10)
            self.assertTrue(ok)
            self.assertTrue(os.path.isfile(path))
            lines = Path(path).read_text(encoding="utf-8").strip().splitlines()
            self.assertLessEqual(len(lines), 10)
            self.assertTrue(os.path.isfile(path + ".1") or len(lines) > 0)


if __name__ == "__main__":
    unittest.main()
