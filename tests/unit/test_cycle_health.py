"""#305 item 5: consecutive price-cycle failures, /health 503, recovery notify."""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch

import core.cycle_health as cycle_health
from core.cycle_health import (
    health_payload,
    mark_cycle_failure,
    mark_cycle_success,
    reset_cycle_health_for_tests,
)


class TestCycleFailureAlerts(unittest.TestCase):
    def setUp(self):
        reset_cycle_health_for_tests()

    def tearDown(self):
        reset_cycle_health_for_tests()

    def test_notifies_once_at_threshold_and_once_on_recovery(self):
        with patch("core.operator_notify.notify_operator", return_value=True) as notify, \
             patch("core.cycle_health._alert_after", return_value=3):
            mark_cycle_failure(RuntimeError("boom1"))
            mark_cycle_failure(RuntimeError("boom2"))
            self.assertEqual(notify.call_count, 0)
            mark_cycle_failure(RuntimeError("boom3"))
            self.assertEqual(notify.call_count, 1)
            mark_cycle_failure(RuntimeError("boom4"))
            self.assertEqual(notify.call_count, 1)
            mark_cycle_success()
            self.assertEqual(notify.call_count, 2)
            mark_cycle_success()
            self.assertEqual(notify.call_count, 2)

    def test_success_records_monotonic_and_wall(self):
        before_mono = time.monotonic()
        before_wall = time.time()
        mark_cycle_success()
        stamp = cycle_health.last_cycle_completed_at
        self.assertIsNotNone(stamp)
        mono, wall = stamp
        self.assertGreaterEqual(mono, before_mono)
        self.assertGreaterEqual(wall, before_wall - 1)
        self.assertEqual(cycle_health.consecutive_cycle_failures, 0)

    def test_health_startup_is_200(self):
        body, status = health_payload(update_interval=60)
        self.assertEqual(status, 200)
        self.assertEqual(body.get("status"), "OK")
        self.assertIsNone(body.get("last_cycle_age_sec"))

    def test_health_503_when_cycle_stale(self):
        mark_cycle_success()
        cycle_health.last_cycle_completed_at = (time.monotonic() - 400, time.time() - 400)
        body, status = health_payload(update_interval=60)
        self.assertEqual(status, 503)
        self.assertGreaterEqual(body["last_cycle_age_sec"], 390)
        self.assertIn("last_cycle_age_sec", body)

    def test_health_200_when_cycle_fresh(self):
        mark_cycle_success()
        body, status = health_payload(update_interval=600)
        self.assertEqual(status, 200)
        self.assertEqual(body.get("status"), "OK")
        self.assertIsNotNone(body.get("last_cycle_age_sec"))
        self.assertLess(body["last_cycle_age_sec"], 10)


class TestAriaBotHealthRoute(unittest.TestCase):
    def setUp(self):
        reset_cycle_health_for_tests()

    def tearDown(self):
        reset_cycle_health_for_tests()

    def test_flask_health_startup_json(self):
        from aria_bot import app

        rv = app.test_client().get("/health")
        self.assertEqual(rv.status_code, 200)
        data = rv.get_json()
        self.assertIsNotNone(data)
        self.assertIn("last_cycle_age_sec", data)
        self.assertIsNone(data["last_cycle_age_sec"])


if __name__ == "__main__":
    unittest.main()
