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

    def test_funding_crowded_long_risk_off(self):
        st, _, why = raw_state_from_features(
            {
                "btc_ret_24h_pct": 0.2,
                "eth_ret_24h_pct": 0.1,
                "btc_trend_4h": 0.0,
                "btc_funding_rate_pct": 0.08,
            }
        )
        self.assertEqual(st, "RISK_OFF")
        self.assertIn("funding_crowded", why)

    def test_funding_crash_with_dump(self):
        st, _, why = raw_state_from_features(
            {
                "btc_ret_24h_pct": -2.5,
                "eth_ret_24h_pct": -2.0,
                "btc_ret_1h_pct": -0.5,
                "btc_trend_4h": -1.0,
                "btc_funding_rate_pct": 0.06,
            }
        )
        self.assertEqual(st, "CRASH")
        self.assertIn("funding_crash", why)

    def test_funding_negative_soft_risk_on(self):
        st, _, why = raw_state_from_features(
            {
                "btc_ret_24h_pct": 0.3,
                "eth_ret_24h_pct": 0.2,
                "btc_ret_1h_pct": 0.1,
                "btc_trend_4h": 1.0,
                "btc_funding_rate_pct": -0.05,
            }
        )
        self.assertEqual(st, "RISK_ON")
        self.assertIn("funding_short_crowded", why)

    def test_funding_missing_fail_open(self):
        st, _, _ = raw_state_from_features(
            {
                "btc_ret_24h_pct": -4.0,
                "eth_ret_24h_pct": -3.0,
            }
        )
        self.assertEqual(st, "RISK_OFF")

    def test_crash_blocks_entries_not_sells_policy(self):
        """CRASH: block new entries / sensors; size 0 — sells are Risk path, not policy."""
        from services.market_oracle.regime import policy_for_state

        pol = policy_for_state("CRASH")
        self.assertTrue(pol["block_new_entries"])
        self.assertTrue(pol["block_sensor_entries"])
        self.assertEqual(pol["size_mult"], 0.0)
        # RISK_OFF still allows new entries (only sensor shadow)
        soft = policy_for_state("RISK_OFF")
        self.assertFalse(soft["block_new_entries"])
        self.assertTrue(soft["block_sensor_entries"])


class TestOracleClientBreadthFunding(unittest.TestCase):
    def test_breadth_empty_universe_fail_open(self):
        from services.market_oracle.client import MarketDataClient

        client = MarketDataClient()
        with patch.object(client, "fetch_all_tickers", return_value=[]):
            out = client.fetch_breadth(top_n=40)
        self.assertEqual(out, {})

    def test_breadth_too_few_liquid_pairs(self):
        from services.market_oracle.client import MarketDataClient

        client = MarketDataClient()
        thin = [
            {
                "currency_pair": f"X{i}_USDT",
                "quote_volume": "100",
                "change_percentage": "1.0",
            }
            for i in range(5)
        ]
        with patch.object(client, "fetch_all_tickers", return_value=thin):
            out = client.fetch_breadth(top_n=40)
        self.assertEqual(out, {})

    def test_funding_gate_success(self):
        from services.market_oracle.client import MarketDataClient

        client = MarketDataClient()

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"funding_rate": "0.0001"}

        with patch.object(client._session, "get", return_value=_Resp()):
            rate, src = client.fetch_btc_funding_rate_pct()
        self.assertEqual(src, "gate")
        self.assertAlmostEqual(rate, 0.01, places=6)

    def test_funding_binance_fallback(self):
        from services.market_oracle.client import MarketDataClient

        client = MarketDataClient()
        calls = {"n": 0}

        class _Fail:
            def raise_for_status(self):
                raise RuntimeError("gate down")

        class _Ok:
            def raise_for_status(self):
                return None

            def json(self):
                return {"lastFundingRate": "0.0002"}

        def _get(url, *a, **k):
            calls["n"] += 1
            if "gateio" in url or "gate" in url:
                return _Fail()
            return _Ok()

        with patch.object(client._session, "get", side_effect=_get):
            rate, src = client.fetch_btc_funding_rate_pct()
        self.assertEqual(src, "binance")
        self.assertAlmostEqual(rate, 0.02, places=6)


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
