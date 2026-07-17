"""Market oracle regime + bot policy tests."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from services.market_oracle.regime import StateHysteresis, decide, raw_state_from_features
from services.market_oracle.snapshot import build_snapshot
from services.market_oracle_ingest import process_market_oracle_ingest
from services.market_oracle_policy import get_market_oracle_policy
from services.market_oracle_store import reset_for_tests, store_snapshot
from services.market_policy_fusion import get_global_market_bias
from services.santiment_store import reset_for_tests as reset_san
from services.santiment_store import store_snapshot as store_san


class TestOracleRegime(unittest.TestCase):
    def test_btc_down_risk_off(self):
        st, _, _ = raw_state_from_features({"btc_ret_24h_pct": -4.0, "eth_ret_24h_pct": -2.0})
        self.assertEqual(st, "RISK_OFF")

    def test_btc_crash(self):
        st, _, _ = raw_state_from_features({"btc_ret_24h_pct": -7.0, "eth_ret_24h_pct": -5.0})
        self.assertEqual(st, "CRASH")

    def test_cascade_1h_crash_even_if_24h_mild(self):
        st, _, why = raw_state_from_features(
            {
                "btc_ret_24h_pct": -2.0,
                "eth_ret_24h_pct": -1.0,
                "btc_ret_1h_pct": -3.0,
                "btc_trend_4h": -1.0,
            }
        )
        self.assertEqual(st, "CRASH")
        self.assertIn("cascade_1h", why)

    def test_risk_on_blocked_when_1h_weak(self):
        st, _, why = raw_state_from_features(
            {
                "btc_ret_24h_pct": 2.0,
                "eth_ret_24h_pct": 1.5,
                "btc_ret_1h_pct": -1.5,
                "btc_trend_4h": 1.0,
            }
        )
        self.assertEqual(st, "NEUTRAL")
        self.assertIn("risk_on_blocked_1h", why)

    def test_risk_on_requires_trend_up(self):
        st, _, _ = raw_state_from_features(
            {
                "btc_ret_24h_pct": 2.0,
                "eth_ret_24h_pct": 1.5,
                "btc_ret_1h_pct": 0.2,
                "btc_trend_4h": 1.0,
            }
        )
        self.assertEqual(st, "RISK_ON")

    def test_structure_4h_risk_off(self):
        st, _, why = raw_state_from_features(
            {
                "btc_ret_24h_pct": -1.0,
                "eth_ret_24h_pct": -0.5,
                "btc_ret_4h_pct": -2.5,
                "btc_trend_4h": -1.0,
            }
        )
        self.assertEqual(st, "RISK_OFF")
        self.assertIn("structure_4h", why)

    def test_hysteresis_holds_until_min_bars(self):
        h = StateHysteresis(min_bars_to_flip=2)
        d1 = decide({"btc_ret_24h_pct": -4.0, "eth_ret_24h_pct": -3.0}, h)
        # first bar of RISK_OFF raw may still hold NEUTRAL if we started NEUTRAL
        d2 = decide({"btc_ret_24h_pct": -4.0, "eth_ret_24h_pct": -3.0}, h)
        self.assertEqual(d2.state, "RISK_OFF")
        self.assertEqual(d2.sensor_policy, "shadow")
        self.assertAlmostEqual(d2.size_mult, 0.35)

    def test_cascade_hysteresis_two_bars(self):
        h = StateHysteresis(min_bars_to_flip=2)
        feat = {
            "btc_ret_24h_pct": -1.0,
            "eth_ret_24h_pct": -0.5,
            "btc_ret_1h_pct": -3.0,
            "btc_trend_4h": -1.0,
        }
        d1 = decide(feat, h)
        self.assertNotEqual(d1.state, "CRASH")  # still flipping
        d2 = decide(feat, h)
        self.assertEqual(d2.state, "CRASH")
        self.assertEqual(d2.size_mult, 0.0)

    def test_breadth_blocks_risk_on(self):
        st, _, why = raw_state_from_features(
            {
                "btc_ret_24h_pct": 2.0,
                "eth_ret_24h_pct": 1.5,
                "btc_ret_1h_pct": 0.2,
                "btc_trend_4h": 1.0,
                "breadth_pct_green": 0.30,
                "breadth_median_24h_pct": -1.5,
            }
        )
        self.assertEqual(st, "NEUTRAL")
        self.assertIn("risk_on_blocked_breadth", why)

    def test_breadth_rotten_risk_off(self):
        st, _, why = raw_state_from_features(
            {
                "btc_ret_24h_pct": 0.5,
                "eth_ret_24h_pct": 0.2,
                "btc_trend_4h": 0.0,
                "breadth_pct_green": 0.20,
                "breadth_median_24h_pct": -3.0,
            }
        )
        self.assertEqual(st, "RISK_OFF")
        self.assertIn("breadth_rotten", why)

    def test_breadth_missing_fail_open_risk_on(self):
        """No breadth keys → price-only path still allows RISK_ON."""
        st, _, _ = raw_state_from_features(
            {
                "btc_ret_24h_pct": 2.0,
                "eth_ret_24h_pct": 1.5,
                "btc_ret_1h_pct": 0.2,
                "btc_trend_4h": 1.0,
            }
        )
        self.assertEqual(st, "RISK_ON")


class TestOraclePolicy(unittest.TestCase):
    def setUp(self):
        reset_for_tests()
        reset_san()

    def tearDown(self):
        reset_for_tests()
        reset_san()

    def test_store_and_policy(self):
        store_snapshot(
            {
                "source": "market_oracle",
                "state": "RISK_OFF",
                "size_mult": 0.35,
                "sensor_policy": "shadow",
                "ttl_sec": 900,
                "rationale": "test",
            }
        )
        pol = get_market_oracle_policy(
            {"architecture": {"market_oracle_risk_enabled": True, "market_oracle_warmup_sec": 0}}
        )
        self.assertTrue(pol["active"])
        self.assertEqual(pol["regime"], "RISK_OFF")
        self.assertEqual(pol["size_mult"], 0.35)

    def test_fusion_min_size(self):
        store_snapshot(
            {
                "source": "market_oracle",
                "state": "NEUTRAL",
                "size_mult": 0.85,
                "sensor_policy": "active",
                "ttl_sec": 900,
            }
        )
        store_san(
            {
                "source": "santiment",
                "regime": "RISK_OFF",
                "size_mult": 0.35,
                "sensor_policy": "shadow",
                "ttl_sec": 1800,
            }
        )
        bias = get_global_market_bias(
            {
                "architecture": {
                    "santiment_risk_enabled": True,
                    "market_oracle_risk_enabled": True,
                    "market_oracle_warmup_sec": 0,
                }
            }
        )
        self.assertTrue(bias["active"])
        self.assertEqual(bias["regime"], "RISK_OFF")
        self.assertAlmostEqual(bias["size_mult"], 0.35)
        self.assertEqual(bias["sensor_policy"], "shadow")
        self.assertIn("santiment", bias["sources"])
        self.assertIn("oracle", bias["sources"])

    def test_ingest(self):
        cfg = {
            "architecture": {
                "market_oracle_ingest_enabled": True,
                "market_oracle_ingest_allow_no_token": True,
            }
        }
        snap = build_snapshot(
            {"btc_ret_24h_pct": -1.0},
            decide(
                {"btc_ret_24h_pct": -1.0, "eth_ret_24h_pct": 0.0, "btc_trend_4h": 1.0},
                StateHysteresis(1),
            ),
        )
        r = process_market_oracle_ingest(snap, config_raw=cfg)
        self.assertTrue(r["ok"])


if __name__ == "__main__":
    unittest.main()
