"""Portfolio plan math + report (0.5%/day linear, 365d horizon)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from services.portfolio_plan import (
    build_report_metrics,
    compute_gap,
    day_index_for,
    plan_nav_at_day,
    plan_series,
)
from services import portfolio_nav_history as nav_hist


class TestPortfolioPlanMath(unittest.TestCase):
    def test_linear_day_0_and_10_and_365(self):
        s = 100_000.0
        self.assertAlmostEqual(plan_nav_at_day(s, 0), 100_000.0)
        self.assertAlmostEqual(plan_nav_at_day(s, 10), 105_000.0)
        self.assertAlmostEqual(plan_nav_at_day(s, 365), 282_500.0)

    def test_clamp_past_horizon(self):
        s = 100_000.0
        self.assertAlmostEqual(plan_nav_at_day(s, 400), plan_nav_at_day(s, 365))

    def test_compound_differs(self):
        s = 100_000.0
        lin = plan_nav_at_day(s, 10, compound=False)
        cmpd = plan_nav_at_day(s, 10, compound=True)
        self.assertNotAlmostEqual(lin, cmpd)
        self.assertGreater(cmpd, lin)

    def test_series_length(self):
        ser = plan_series(1000, horizon_days=365)
        self.assertEqual(len(ser), 366)
        self.assertAlmostEqual(ser[0], 1000)
        self.assertAlmostEqual(ser[-1], 1000 * 2.825)

    def test_day_index(self):
        start = date(2026, 1, 1)
        self.assertEqual(day_index_for(start, date(2026, 1, 1)), 0)
        self.assertEqual(day_index_for(start, date(2026, 1, 11)), 10)
        self.assertEqual(day_index_for(start, date(2027, 1, 2), horizon_days=365), 365)

    def test_gap_negative(self):
        g = compute_gap(100_000, 99_000, 10)
        self.assertAlmostEqual(g.nav_plan, 105_000)
        self.assertAlmostEqual(g.delta_usd, 99_000 - 105_000)
        self.assertEqual(g.days_remaining, 355)
        self.assertEqual(g.horizon_days, 365)


class TestNavHistory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "nav.json"
        self.patcher = patch.object(
            nav_hist,
            "_json_path",
            return_value=self.path,
        )
        self.patcher.start()
        self.mongo = patch.object(nav_hist, "_use_mongo", return_value=False)
        self.mongo.start()

    def tearDown(self):
        self.mongo.stop()
        self.patcher.stop()

    def test_record_and_load(self):
        with patch.object(nav_hist, "_tenant_scope", return_value=("default", "demo")):
            p1 = nav_hist.record_nav_snapshot(
                nav=100_000,
                cash=100_000,
                positions_mtm=0,
                initial_capital=100_000,
                on_date=date(2026, 7, 1),
            )
            self.assertEqual(p1["date"], "2026-07-01")
            p2 = nav_hist.record_nav_snapshot(
                nav=101_000,
                cash=50_000,
                positions_mtm=51_000,
                initial_capital=100_000,
                on_date=date(2026, 7, 2),
            )
            # upsert same day
            nav_hist.record_nav_snapshot(
                nav=102_000,
                cash=50_000,
                positions_mtm=52_000,
                initial_capital=100_000,
                on_date=date(2026, 7, 2),
            )
            hist = nav_hist.load_nav_history()
        self.assertEqual(len(hist), 2)
        self.assertEqual(hist[1]["nav"], 102_000.0)

    def test_history_as_day_map(self):
        start = date(2026, 7, 1)
        points = [
            {"date": "2026-07-01", "nav": 100000},
            {"date": "2026-07-03", "nav": 101000},
        ]
        m = nav_hist.history_as_day_nav_map(points, start)
        self.assertEqual(m[0], 100000)
        self.assertEqual(m[2], 101000)


class TestPlanCommand(unittest.TestCase):
    def test_handle_routes(self):
        from notifications.telegram_commands import plan_commands

        with patch.object(plan_commands, "send_plan_report") as send:
            self.assertTrue(plan_commands.handle("/plan"))
            self.assertTrue(plan_commands.handle("/performance"))
            self.assertEqual(send.call_count, 2)
            self.assertFalse(plan_commands.handle("/orders"))

    def test_format_report_contains_horizon(self):
        from notifications.telegram_commands.plan_commands import format_plan_report_html

        gap = compute_gap(100_000, 99_000, 42)
        html = format_plan_report_html(gap=gap, plan_start=date(2026, 6, 1))
        self.assertIn("365", html)
        self.assertIn("42", html)
        self.assertIn("Plan-Ende", html)
        self.assertIn("99000", html.replace(",", ""))


class TestPlanChart(unittest.TestCase):
    def test_render_png(self):
        from notifications.plan_chart import render_plan_vs_actual_png

        path = render_plan_vs_actual_png(
            start_capital=100_000,
            plan_start=date(2026, 1, 1),
            actual_by_day={0: 100_000, 5: 101_000, 10: 99_500},
            today_day_index=10,
            horizon_days=365,
        )
        if path is None:
            self.skipTest("matplotlib unavailable")
        try:
            self.assertTrue(Path(path).is_file())
            self.assertGreater(Path(path).stat().st_size, 1000)
        finally:
            Path(path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
