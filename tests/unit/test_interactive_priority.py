from __future__ import annotations

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.interactive_priority import (
    interactive_pending,
    interactive_priority,
    reset_interactive_priority_for_tests,
    yield_to_interactive,
)


class TestInteractivePriority(unittest.TestCase):
    def tearDown(self):
        reset_interactive_priority_for_tests()

    def test_idle_yield_returns_immediately(self):
        t0 = time.monotonic()
        yield_to_interactive(max_wait=1.0)
        self.assertLess(time.monotonic() - t0, 0.2)
        self.assertFalse(interactive_pending())

    def test_yield_waits_until_released(self):
        released = []

        def holder():
            with interactive_priority(ttl_sec=5):
                time.sleep(0.12)
                released.append(True)

        th = threading.Thread(target=holder)
        th.start()
        time.sleep(0.02)
        t0 = time.monotonic()
        yield_to_interactive(max_wait=1.0, poll=0.01)
        elapsed = time.monotonic() - t0
        th.join(timeout=1.0)
        self.assertTrue(released)
        self.assertGreaterEqual(elapsed, 0.06)

    def test_nested_contexts(self):
        with interactive_priority(ttl_sec=5):
            self.assertTrue(interactive_pending())
            with interactive_priority(ttl_sec=5):
                self.assertTrue(interactive_pending())
            self.assertTrue(interactive_pending())
        self.assertFalse(interactive_pending())

    def test_ttl_expires(self):
        with interactive_priority(ttl_sec=0.05):
            time.sleep(0.08)
            self.assertFalse(interactive_pending())


if __name__ == "__main__":
    unittest.main()
