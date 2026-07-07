import os
import sys
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from services import entry_sensor_loop
from strategies import watch_15m_state


class TestEntrySensorWebhookPriority(unittest.TestCase):
    def setUp(self):
        watch_15m_state.clear_all_watches_for_tests()
        entry_sensor_loop.reset_poll_state_for_tests()

    def test_webhook_watch_sorted_first(self):
        watch_15m_state.set_watch("AAA/USDT", "4h", reason="watchlist")
        watch_15m_state.set_watch("ZZZ/USDT", "4h", reason="webhook:tradingview:volume_spike", webhook_source="tradingview")
        ordered = entry_sensor_loop._sort_watched_for_poll(watch_15m_state.list_watched())
        self.assertEqual(ordered[0]["symbol"], "ZZZ/USDT")

    def test_priority_poll_bypasses_gap(self):
        cfg = {"poll_interval_sec": 20, "webhook_priority_poll": True}
        symbol = "VELVET/USDT"
        watch_15m_state.set_watch(symbol, "4h", reason="webhook:tv", priority_poll=True)
        now = time.monotonic()
        self.assertTrue(entry_sensor_loop._should_poll_symbol(symbol, cfg, now))
        self.assertFalse(entry_sensor_loop._should_poll_symbol(symbol, cfg, now))


if __name__ == "__main__":
    unittest.main()