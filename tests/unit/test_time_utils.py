import os
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.time_utils import (
    display_timezone_name,
    format_display_with_zone,
    format_operator_time,
    ledger_datetime_utc,
    now_display,
    operator_timezone_name,
    operator_tz,
    process_local_tz,
    to_operator_time,
    to_utc,
    utc_now,
)


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

    def test_operator_timezone_defaults_berlin(self):
        self.assertEqual(operator_timezone_name(), "Europe/Berlin")
        self.assertEqual(str(operator_tz()), "Europe/Berlin")

    def test_format_operator_time_naive_is_operator_wallclock(self):
        dt = datetime(2026, 6, 7, 19, 16, 8)
        self.assertEqual(format_operator_time(dt, "%d.%m.%Y %H:%M"), "07.06.2026 19:16")
        self.assertEqual(
            format_operator_time("2026-06-07T19:16:08", "%d.%m.%Y %H:%M"),
            "07.06.2026 19:16",
        )

    def test_format_operator_time_converts_aware_utc(self):
        dt = datetime(2026, 6, 7, 17, 16, 8, tzinfo=timezone.utc)
        self.assertEqual(format_operator_time(dt, "%d.%m.%Y %H:%M"), "07.06.2026 19:16")
        self.assertEqual(
            format_operator_time("2026-06-07T17:16:08+00:00", "%d.%m.%Y %H:%M"),
            "07.06.2026 19:16",
        )

    def test_to_operator_time_naive_stays_berlin_on_any_host_tz(self):
        naive = datetime(2026, 6, 7, 19, 16, 8)
        old = os.environ.get("TZ")
        try:
            for tz_name in ("UTC", "Europe/Berlin", "Asia/Tokyo"):
                os.environ["TZ"] = tz_name
                time.tzset()
                got = to_operator_time(naive)
                self.assertEqual(got.tzinfo, ZoneInfo("Europe/Berlin"))
                self.assertEqual(got.replace(tzinfo=None), naive)
                self.assertEqual(
                    format_operator_time(naive, "%d.%m.%Y %H:%M"),
                    "07.06.2026 19:16",
                )
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()

    def test_ledger_datetime_utc_naive_is_process_local(self):
        naive = datetime(2026, 6, 7, 19, 16, 8)
        old = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "UTC"
            time.tzset()
            utc = ledger_datetime_utc(naive)
            self.assertEqual(utc, datetime(2026, 6, 7, 19, 16, 8, tzinfo=timezone.utc))

            os.environ["TZ"] = "Europe/Berlin"
            time.tzset()
            berlin = ledger_datetime_utc(naive)
            self.assertEqual(
                berlin,
                datetime(2026, 6, 7, 19, 16, 8, tzinfo=ZoneInfo("Europe/Berlin")).astimezone(
                    timezone.utc
                ),
            )
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()

    def test_process_local_tz_uses_iana_zone_for_dst(self):
        old = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "Europe/Berlin"
            time.tzset()
            tz = process_local_tz()
            self.assertEqual(getattr(tz, "key", None) or str(tz), "Europe/Berlin")
            winter = datetime(2026, 1, 15, 12, 0, 0, tzinfo=tz)
            summer = datetime(2026, 7, 15, 12, 0, 0, tzinfo=tz)
            self.assertEqual(winter.utcoffset(), timedelta(hours=1))
            self.assertEqual(summer.utcoffset(), timedelta(hours=2))
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()

    def test_to_utc_keeps_aware_offset(self):
        aware = datetime(2026, 6, 7, 19, 16, 8, tzinfo=ZoneInfo("Europe/Berlin"))
        self.assertEqual(to_utc(aware), aware.astimezone(timezone.utc))

    def test_utc_now_is_aware(self):
        now = utc_now()
        self.assertIsNotNone(now.tzinfo)
        self.assertEqual(now.utcoffset().total_seconds(), 0)


if __name__ == "__main__":
    unittest.main()
