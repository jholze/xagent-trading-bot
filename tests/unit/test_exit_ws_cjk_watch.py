"""#558 — CJK symbols stay in the exit_ws watch set.

update_watch_set uppercases the pair. 龙虾/USDT must still be returned.
"""

from __future__ import annotations

import unittest

from services.exit_realtime.hub import ExitRealtimeHub


class TestExitWsCjkWatch(unittest.TestCase):
    def test_lobster_usdt_stays_in_watch_set(self):
        hub = ExitRealtimeHub(
            {"exit_realtime": {"enabled": False}, "correlated_tier": {"enabled": False}}
        )
        watched = hub.update_watch_set(["龙虾/USDT"])
        self.assertIn("龙虾/USDT", watched)


if __name__ == "__main__":
    unittest.main()
