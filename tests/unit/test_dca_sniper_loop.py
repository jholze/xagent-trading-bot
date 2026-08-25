"""DCA sniper poll loop: wake must be consumed before the cycle, not after."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from services.dca_sniper.loop import DcaSniperLoop


class TestDcaSniperLoop(unittest.TestCase):
    def test_wake_cleared_before_wait(self):
        loop = DcaSniperLoop(
            config={"dca_sniper": {"enabled": False, "poll_interval_sec": 15}}
        )
        order: list[str] = []
        real_clear = loop._wake.clear

        def clear():
            order.append("clear")
            real_clear()

        def wait(timeout=None):
            order.append("wait")
            loop._stop.set()
            return True

        with patch.object(loop._wake, "clear", side_effect=clear):
            with patch.object(loop._wake, "wait", side_effect=wait):
                with patch(
                    "services.dca_sniper.loop.dca_sniper_enabled", return_value=False
                ):
                    loop._run()
        self.assertGreaterEqual(len(order), 2)
        self.assertEqual(order[0], "clear")
        self.assertEqual(order[1], "wait")
