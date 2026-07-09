import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.time_utils import display_timezone_name, format_display_with_zone, now_display


class TestTimeUtils(unittest.TestCase):
    def test_default_timezone_berlin(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("BOT_TIMEZONE", None)
            os.environ.pop("TZ", None)
        self.assertEqual(display_timezone_name(), "Europe/Berlin")

    def test_now_display_has_tzinfo(self):
        dt = now_display()
        self.assertIsNotNone(dt.tzinfo)

    def test_format_includes_zone_label(self):
        text = format_display_with_zone("%H:%M")
        self.assertRegex(text, r"\d{2}:\d{2} (CET|CEST|Europe/Berlin)")


if __name__ == "__main__":
    unittest.main()