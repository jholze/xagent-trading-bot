import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from notifications.telegram_commands.lc_commands import _lc_unavailable_message


class TestLcCommands(unittest.TestCase):
    def _cfg(self, **lc_overrides):
        cfg = MagicMock()
        cfg.lunarcrush_config = {
            "enabled": True,
            "use_mock": False,
            "api_key_env": "LUNARCRUSH_API_KEY",
            "signal_ttl_hours": 4,
            "use_list_endpoint": False,
            "thresholds": {"buy_galaxy_min": 52, "buy_sentiment_min": 55},
            **lc_overrides,
        }
        return cfg

    def test_disabled_message(self):
        msg = _lc_unavailable_message(self._cfg(enabled=False))
        self.assertIn("deaktiviert", msg)

    def test_no_metrics_message(self):
        msg = _lc_unavailable_message(self._cfg(), metrics_count=0)
        self.assertIn("Keine LunarCrush-Daten", msg)
        self.assertNotIn("Enable lunarcrush.enabled", msg)

    def test_threshold_message_when_metrics_present(self):
        msg = _lc_unavailable_message(self._cfg(), metrics_count=12)
        self.assertIn("12", msg)
        self.assertIn("kein BUY/SELL", msg)


if __name__ == "__main__":
    unittest.main()