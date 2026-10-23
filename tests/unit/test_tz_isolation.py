"""TZ must not survive the test that set it.

``process_local_tz()`` reads ``TZ``. On the CI runner, naive
``datetime.now()`` stays UTC unless the test also calls ``time.tzset()``.
Leaving ``TZ=Europe/Berlin`` makes later order-day tests tag those UTC
stamps as Berlin, so "today" is missed between 22:00 and 24:00 UTC.

``test_t26e_short_timezones_summer_and_winter`` restores ``TZ`` itself.
``restore_process_tz`` in ``tests/conftest.py`` is the backstop when a
future test forgets. The ordered pair below fails if that backstop stops
restoring. The names are alphabetical on purpose.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
import unittest
from pathlib import Path

_LEAKED_TZ = "Pacific/Kiritimati"


def _issue_651_module():
    path = Path(__file__).resolve().parent / "test_issue_651_stale_peak.py"
    for loaded in sys.modules.values():
        loaded_path = getattr(loaded, "__file__", None)
        if loaded_path and Path(loaded_path).resolve() == path:
            return loaded
    name = "test_issue_651_stale_peak"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class TestTzDoesNotLeak(unittest.TestCase):
    def test_k3_short_timezone_check_restores_tz(self):
        module = _issue_651_module()
        before = os.environ.get("TZ")
        case = module.TestIssue651K3("test_t26e_short_timezones_summer_and_winter")
        case.test_t26e_short_timezones_summer_and_winter()
        self.assertEqual(os.environ.get("TZ"), before)

    def test_01_sets_tz_without_restoring(self):
        os.environ["TZ"] = _LEAKED_TZ

    def test_02_previous_test_did_not_leak_tz(self):
        leaked = os.environ.get("TZ") == _LEAKED_TZ
        if leaked:
            os.environ.pop("TZ", None)
            if hasattr(time, "tzset"):
                time.tzset()
        self.assertFalse(leaked)
