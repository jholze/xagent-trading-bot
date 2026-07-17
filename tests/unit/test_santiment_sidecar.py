"""Santiment sidecar + bot ingest unit tests."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from services.santiment_ingest import process_santiment_ingest, santiment_token_ok
from services.santiment_policy import get_santiment_policy
from services.santiment_sidecar.regime import decide_regime, should_push
from services.santiment_sidecar.snapshot import build_snapshot
from services.santiment_store import get_latest_snapshot, reset_for_tests, snapshot_is_fresh, store_snapshot


class TestSantimentRegime(unittest.TestCase):
    def test_empty_features_neutral(self):
        d = decide_regime({})
        self.assertEqual(d.regime, "NEUTRAL")
        self.assertEqual(d.size_mult, 1.0)

    def test_social_collapse_risk_off(self):
        d = decide_regime(
            {
                "btc_social_volume_delta_1d": -0.4,
                "eth_social_volume_delta_1d": -0.4,
            }
        )
        self.assertEqual(d.regime, "RISK_OFF")
        self.assertLess(d.size_mult, 0.5)
        self.assertEqual(d.sensor_policy, "shadow")

    def test_severe_collapse_crash(self):
        d = decide_regime(
            {
                "btc_social_volume_delta_1d": -0.7,
                "eth_social_volume_delta_1d": -0.6,
            }
        )
        self.assertEqual(d.regime, "CRASH")
        self.assertEqual(d.sensor_policy, "block")

    def test_should_push_on_regime_change(self):
        a = {"regime": "NEUTRAL", "size_mult": 1.0, "sensor_policy": "active"}
        b = {"regime": "RISK_OFF", "size_mult": 0.35, "sensor_policy": "shadow"}
        self.assertTrue(should_push(a, b))
        self.assertFalse(should_push(b, dict(b)))


class TestSantimentIngest(unittest.TestCase):
    def setUp(self):
        reset_for_tests()

    def tearDown(self):
        reset_for_tests()

    def test_token_from_env(self):
        with patch.dict(os.environ, {"SANTIMENT_INGEST_TOKEN": "sec"}, clear=False):
            self.assertTrue(santiment_token_ok("sec", {}))
            self.assertFalse(santiment_token_ok("bad", {}))

    def test_process_stores_snapshot(self):
        cfg = {
            "architecture": {
                "santiment_ingest_enabled": True,
                "santiment_ingest_allow_no_token": True,
            }
        }
        snap = build_snapshot(
            {"btc_social_volume_delta_1d": -0.4, "eth_social_volume_delta_1d": -0.4}
        )
        with patch.dict(os.environ, {}, clear=False):
            result = process_santiment_ingest(snap, config_raw=cfg)
        self.assertTrue(result["ok"])
        stored = get_latest_snapshot(allow_redis=False)
        self.assertIsNotNone(stored)
        self.assertEqual(stored["regime"], "RISK_OFF")
        self.assertTrue(snapshot_is_fresh(stored))


class TestSantimentPolicy(unittest.TestCase):
    def setUp(self):
        reset_for_tests()

    def tearDown(self):
        reset_for_tests()

    def test_fail_open_without_snapshot(self):
        with patch("services.santiment_policy.get_latest_snapshot", return_value=None):
            pol = get_santiment_policy({"architecture": {"santiment_risk_enabled": True}})
        self.assertFalse(pol["active"])
        self.assertEqual(pol["size_mult"], 1.0)
        self.assertFalse(pol["block_buys"])

    def test_risk_off_policy_from_snapshot(self):
        store_snapshot(
            {
                "source": "santiment",
                "regime": "RISK_OFF",
                "size_mult": 0.35,
                "sensor_policy": "shadow",
                "ttl_sec": 1800,
                "rationale": "test",
            }
        )
        pol = get_santiment_policy(
            {
                "architecture": {
                    "santiment_risk_enabled": True,
                    "santiment_apply_size_mult": True,
                    "santiment_apply_sensor_policy": True,
                }
            }
        )
        self.assertTrue(pol["active"])
        self.assertEqual(pol["regime"], "RISK_OFF")
        self.assertEqual(pol["size_mult"], 0.35)
        self.assertEqual(pol["sensor_policy"], "shadow")
        self.assertFalse(pol["block_buys"])

    def test_crash_blocks_buys(self):
        store_snapshot(
            {
                "source": "santiment",
                "regime": "CRASH",
                "size_mult": 0.0,
                "sensor_policy": "block",
                "ttl_sec": 1800,
            }
        )
        pol = get_santiment_policy({"architecture": {"santiment_risk_enabled": True}})
        self.assertTrue(pol["block_buys"])


class TestSantimentRoute(unittest.TestCase):
    def setUp(self):
        reset_for_tests()
        from aria_bot import app

        self.client = app.test_client()

    def tearDown(self):
        reset_for_tests()

    def test_ingest_route(self):
        snap = build_snapshot({"btc_social_volume_delta_1d": 0.1, "eth_social_volume_delta_1d": 0.0})
        with patch.dict(os.environ, {"SANTIMENT_INGEST_TOKEN": "t1"}, clear=False), \
             patch("services.santiment_ingest.santiment_ingest_enabled", return_value=True):
            resp = self.client.post(
                "/api/santiment/ingest",
                json=snap,
                headers={"X-Santiment-Token": "t1"},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json().get("ok"))


if __name__ == "__main__":
    unittest.main()
