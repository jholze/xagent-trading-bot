"""Unit tests for pure demo snapshot report builder + thin CLI integration."""

import io
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from services.demo_snapshot_report import (
    EXPECTED_OPEN_POSITIONS,
    build_report_from_reads,
)


class TestBuildReportFromReads(unittest.TestCase):
    def _base_reads(self):
        orders = {
            "ledger_scope": "demo",
            "orders": [
                {
                    "id": "o1",
                    "status": "filled",
                    "side": "buy",
                    "symbol": "ARIA/USDT",
                    "timeframe": "4h",
                    "execution": {"price": 1.0, "amount": 100.0},
                }
            ],
        }
        mongo_pos = {
            "ledger_scope": "demo",
            "positions": {
                "CACHE_USDT_4h": {"amount": 10.0},
                "BTC_USDT_4h": {"amount": 1.0},
            },
        }
        return orders, mongo_pos

    def test_pure_builder_open_positions_from_reads(self):
        orders, mongo_pos = self._base_reads()
        report = build_report_from_reads(
            demo_orders=orders,
            demo_positions={"positions": {}},
            demo_history={"trades": []},
            mongo_orders_before=orders,
            mongo_positions_before=mongo_pos,
            mongo_positions_after=mongo_pos,
            mongo_history_before={"trades": []},
            dry_run=True,
            from_live=False,
            database="xagent_test",
            open_positions=EXPECTED_OPEN_POSITIONS,
            load_positions_keys=EXPECTED_OPEN_POSITIONS,
            equity_metrics={
                "equity_nav": 100_000.0,
                "nav_day_start": 99_500.0,
                "nav_delta": 500.0,
            },
        )
        self.assertEqual(report["open_positions"], EXPECTED_OPEN_POSITIONS)
        self.assertEqual(report["load_positions_keys"], EXPECTED_OPEN_POSITIONS)
        self.assertTrue(report["positions_mongo_unchanged"])
        self.assertTrue(report["roundtrip"]["positions_unchanged"])
        self.assertEqual(report["invariant_violations"], [])

    def test_dry_run_detects_mongo_positions_drift(self):
        orders, mongo_before = self._base_reads()
        mongo_after = {
            "ledger_scope": "demo",
            "positions": {"CHANGED_USDT_4h": {"amount": 1.0}},
        }
        report = build_report_from_reads(
            demo_orders=orders,
            demo_positions={"positions": {}},
            demo_history=None,
            mongo_orders_before=orders,
            mongo_positions_before=mongo_before,
            mongo_positions_after=mongo_after,
            mongo_orders_after=orders,
            mongo_history_before=None,
            dry_run=True,
            from_live=False,
            database="xagent_test",
            open_positions=EXPECTED_OPEN_POSITIONS,
            load_positions_keys=EXPECTED_OPEN_POSITIONS,
        )
        self.assertFalse(report["positions_mongo_unchanged"])
        self.assertFalse(report["roundtrip"]["positions_unchanged"])

    def test_dry_run_detects_mongo_orders_drift(self):
        orders, mongo_pos = self._base_reads()
        mongo_stale = dict(orders)
        mongo_stale["orders"] = list(orders.get("orders", [])) + [{"id": "x", "status": "filled"}]
        report = build_report_from_reads(
            demo_orders=orders,
            demo_positions={"positions": {}},
            demo_history=None,
            mongo_orders_before=mongo_stale,
            mongo_orders_after=mongo_stale,
            mongo_positions_before=mongo_pos,
            mongo_positions_after=mongo_pos,
            mongo_history_before=None,
            dry_run=True,
            from_live=False,
            database="xagent_test",
            open_positions=EXPECTED_OPEN_POSITIONS,
            load_positions_keys=EXPECTED_OPEN_POSITIONS,
        )
        self.assertFalse(report["roundtrip"]["orders"])

    def test_manual_test_coins_flagged(self):
        orders, mongo_pos = self._base_reads()
        orders["orders"].append(
            {"status": "filled", "symbol": "XRVM/USDT", "timeframe": "4h"}
        )
        report = build_report_from_reads(
            demo_orders=orders,
            demo_positions={"positions": {}},
            demo_history=None,
            mongo_orders_before=orders,
            mongo_positions_before=mongo_pos,
            mongo_positions_after=mongo_pos,
            mongo_history_before=None,
            dry_run=True,
            from_live=False,
            database="xagent_test",
            open_positions=26,
            load_positions_keys=26,
        )
        joined = "; ".join(report["invariant_violations"])
        self.assertIn("manual test coins", joined)


class TestSnapshotCliIntegration(unittest.TestCase):
    def test_dry_run_cli_exit_zero_and_invariants(self):
        """Exercise CLI main() without depending on operator demo ledger drift."""
        from scripts.mongo_snapshot_demo import main

        equity = {
            "equity_nav": 100_000.0,
            "nav_day_start": 99_500.0,
            "nav_delta": 500.0,
            "open_positions": EXPECTED_OPEN_POSITIONS,
            "load_positions_keys": EXPECTED_OPEN_POSITIONS,
        }
        buf = io.StringIO()
        with patch.dict(os.environ, {"MONGODB_DB": "xagent_test"}, clear=False), \
             patch("services.demo_snapshot_report._equity_metrics", return_value=equity), \
             patch(
                 "services.demo_snapshot_report.count_open_positions_from_orders",
                 return_value=EXPECTED_OPEN_POSITIONS,
             ), \
             patch.object(sys, "argv", ["mongo_snapshot_demo.py", "--dry-run", "--test-db"]), \
             patch("sys.stdout", buf):
            rc = main()

        out = buf.getvalue()
        self.assertEqual(rc, 0, out)
        self.assertIn("[invariants-ok]", out)
        self.assertIn("[migrate]", out)
        self.assertIn(f"open_positions={EXPECTED_OPEN_POSITIONS}", out)
        self.assertIn(f"load_positions_keys={EXPECTED_OPEN_POSITIONS}", out)


if __name__ == "__main__":
    unittest.main()