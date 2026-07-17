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

    def test_hysteresis_holds_until_min_bars(self):
        h = StateHysteresis(min_bars_to_flip=2)
        d1 = decide({"btc_ret_24h_pct": -4.0, "eth_ret_24h_pct": -3.0}, h)
        # first bar of RISK_OFF raw may still hold NEUTRAL if we started NEUTRAL
        d2 = decide({"btc_ret_24h_pct": -4.0, "eth_ret_24h_pct": -3.0}, h)
        self.assertEqual(d2.state, "RISK_OFF")
        self.assertEqual(d2.sensor_policy, "shadow")
        self.assertAlmostEqual(d2.size_mult, 0.35)


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
