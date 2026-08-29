"""Santiment sidecar + bot ingest unit tests."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from services.santiment_ingest import process_santiment_ingest, santiment_token_ok
from services.market_policy_fusion import (
    apply_global_mode_bias,
    get_global_market_bias,
    inject_global_sentiment,
)
from services.santiment_policy import get_santiment_policy
from services.santiment_sidecar.client import is_series_fresh, series_lag_days
from services.santiment_sidecar.regime import RISK_ON_SIZE_CAP, decide_regime, should_push
from services.santiment_sidecar.snapshot import build_snapshot
from services.santiment_store import (
    get_latest_snapshot,
    reset_for_tests,
    snapshot_is_fresh,
    status_line,
    store_snapshot,
)
from strategies.trading_modes import MODE_DEFENSIVE, MODE_GRID, MODE_HYBRID, MODE_MOMENTUM


class TestSantimentRegime(unittest.TestCase):
    def test_empty_features_neutral(self):
        d = decide_regime({})
        self.assertEqual(d.regime, "NEUTRAL")
        self.assertEqual(d.size_mult, 1.0)

    def test_social_only_without_fresh_flag_fail_open(self):
        """Lagged social must not drive RISK_OFF/CRASH."""
        d = decide_regime(
            {
                "btc_social_volume_delta_1d": -0.7,
                "eth_social_volume_delta_1d": -0.6,
            },
            meta={"social_fresh": False},
        )
        self.assertEqual(d.regime, "NEUTRAL")
        self.assertEqual(d.size_mult, 1.0)

    def test_daa_vol_crash(self):
        d = decide_regime(
            {
                "btc_daa_delta_1d": -0.4,
                "eth_daa_delta_1d": -0.35,
                "btc_vol_1d": 0.09,
                "eth_vol_1d": 0.07,
            }
        )
        self.assertEqual(d.regime, "CRASH")
        self.assertEqual(d.sensor_policy, "block")
        self.assertEqual(d.size_mult, 0.0)

    def test_daa_collapse_risk_off(self):
        d = decide_regime(
            {
                "btc_daa_delta_1d": -0.35,
                "eth_daa_delta_1d": -0.3,
                "btc_vol_1d": 0.03,
                "eth_vol_1d": 0.03,
            }
        )
        self.assertEqual(d.regime, "RISK_OFF")
        self.assertLessEqual(d.size_mult, 0.5)
        self.assertEqual(d.sensor_policy, "shadow")

    def test_soft_risk_on_size_capped(self):
        d = decide_regime(
            {
                "btc_daa_delta_1d": 0.2,
                "eth_daa_delta_1d": 0.15,
                "btc_vol_1d": 0.01,
                "eth_vol_1d": 0.012,
            }
        )
        self.assertEqual(d.regime, "RISK_ON")
        self.assertLessEqual(d.size_mult, RISK_ON_SIZE_CAP)
        self.assertEqual(d.size_mult, RISK_ON_SIZE_CAP)

    def test_fresh_social_soft_bias_into_risk_off(self):
        base = {
            "btc_daa_delta_1d": -0.2,
            "eth_daa_delta_1d": -0.18,
            "btc_vol_1d": 0.042,
            "eth_vol_1d": 0.04,
        }
        without = decide_regime(base, meta={"social_fresh": False})
        with_social = decide_regime(
            {
                **base,
                "btc_social_volume_delta_1d": -0.4,
                "eth_social_volume_delta_1d": -0.4,
            },
            meta={"social_fresh": True, "policy_inputs": ["daa", "vol", "social"]},
        )
        self.assertEqual(with_social.regime, "RISK_OFF")
        self.assertGreaterEqual(without.size_mult, with_social.size_mult)

    def test_dev_soft_bias(self):
        mild = {
            "btc_daa_delta_1d": -0.15,
            "eth_daa_delta_1d": -0.12,
            "btc_vol_1d": 0.03,
        }
        with_dev = {
            **mild,
            "btc_dev_activity_delta_1d": -0.4,
            "eth_dev_activity_delta_1d": -0.35,
        }
        d0 = decide_regime(mild)
        d1 = decide_regime(with_dev)
        self.assertIn(d1.regime, ("NEUTRAL", "RISK_OFF"))
        # Dev drag should not improve composite/size vs DAA-only
        self.assertGreaterEqual(
            (d0.scores.get("onchain") or 0),
            (d1.scores.get("onchain") or 0),
        )

    def test_should_push_on_regime_change(self):
        a = {"regime": "NEUTRAL", "size_mult": 1.0, "sensor_policy": "active"}
        b = {"regime": "RISK_OFF", "size_mult": 0.35, "sensor_policy": "shadow"}
        self.assertTrue(should_push(a, b))
        self.assertFalse(should_push(b, dict(b)))


class TestSantimentClientHelpers(unittest.TestCase):
    def test_series_freshness(self):
        now = datetime.now(timezone.utc)
        fresh = [{"datetime": (now - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ"), "value": 1}]
        stale = [{"datetime": (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"), "value": 1}]
        self.assertTrue(is_series_fresh(fresh, now=now))
        self.assertFalse(is_series_fresh(stale, now=now))
        self.assertLess(series_lag_days(fresh, now=now), 1.0)
        self.assertGreater(series_lag_days(stale, now=now), 20.0)

    def test_lean_fetch_stops_on_rate_limit(self):
        """Thrifty: abort remaining metrics after first 429 (don't burn quota)."""
        from services.santiment_sidecar.client import RateLimitError, SantimentClient

        client = SantimentClient(
            "test-key",
            inter_request_delay_sec=0,
            abort_on_rate_limit=True,
            fetch_social=False,
            fetch_leverage=False,
            fetch_dev=False,
        )
        calls = {"n": 0}

        def fake_ts(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                now = datetime.now(timezone.utc)
                return [
                    {
                        "datetime": (now - timedelta(hours=12)).strftime(
                            "%Y-%m-%dT%H:%M:%SZ"
                        ),
                        "value": 100.0,
                    },
                    {
                        "datetime": (now - timedelta(hours=6)).strftime(
                            "%Y-%m-%dT%H:%M:%SZ"
                        ),
                        "value": 110.0,
                    },
                ]
            raise RateLimitError("Santiment rate limited (429)", retry_after_sec=3600)

        with patch.object(client, "get_metric_timeseries", side_effect=fake_ts):
            result = client.fetch_features()
        # lean has 4 metrics; after ok+429 should stop (2 calls not 4)
        self.assertEqual(calls["n"], 2)
        self.assertTrue(result.meta.get("rate_limited"))
        self.assertEqual(result.meta.get("rate_limit_retry_sec"), 3600)
        self.assertEqual(result.meta.get("metric_profile"), "lean")
        self.assertIn("btc_daa", result.meta.get("metrics_ok") or [])

    def test_config_lean_defaults(self):
        from services.santiment_sidecar.config import load_config

        with patch.dict(os.environ, {}, clear=False):
            # clear thrifty overrides if any
            for k in (
                "SANTIMENT_METRIC_PROFILE",
                "POLL_INTERVAL_SEC",
                "SANTIMENT_FETCH_SOCIAL",
                "SANTIMENT_FETCH_LEVERAGE",
                "SANTIMENT_FETCH_DEV",
            ):
                os.environ.pop(k, None)
            cfg = load_config()
        self.assertEqual(cfg["metric_profile"], "lean")
        self.assertFalse(cfg["fetch_social"])
        self.assertFalse(cfg["fetch_leverage"])
        self.assertFalse(cfg["fetch_dev"])
        self.assertGreaterEqual(cfg["poll_interval_sec"], 1800)


class TestSantimentLeanAndAsset(unittest.TestCase):
    """Lean profile (staging #237) + sniper micro asset fetch (deep-memory)."""

    def _fresh_series(self, v0=100.0, v1=110.0):
        now = datetime.now(timezone.utc)
        return [
            {
                "datetime": (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "value": v0,
            },
            {
                "datetime": (now - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "value": v1,
            },
        ]

    def test_lean_fetch_four_core_no_social(self):
        from services.santiment_sidecar.client import SantimentClient

        client = SantimentClient(
            "fake-key",
            inter_request_delay_sec=0,
            fetch_social=False,
            fetch_leverage=False,
            fetch_dev=False,
        )
        calls = {"n": 0}

        def fake_ts(**kwargs):
            calls["n"] += 1
            return self._fresh_series()

        with patch.object(client, "get_metric_timeseries", side_effect=fake_ts):
            res = client.fetch_features()
        self.assertEqual(calls["n"], 4)
        self.assertEqual(res.meta.get("metric_profile"), "lean")
        self.assertIn("btc_daa", res.meta["metrics_ok"])
        self.assertIn("eth_vol_1d", res.meta["metrics_ok"])
        self.assertNotIn("btc_social_volume", res.meta["metrics_ok"])
        self.assertNotIn("social", res.meta.get("policy_inputs") or [])

    def test_lean_no_leverage_research_double_fetch(self):
        from services.santiment_sidecar.client import SantimentClient

        client = SantimentClient(
            "fake-key",
            inter_request_delay_sec=0,
            fetch_social=False,
            fetch_leverage=True,
            fetch_dev=False,
            leverage_research_fallback=False,
        )
        calls = {"n": 0}

        def fake_ts(**kwargs):
            calls["n"] += 1
            metric = kwargs.get("metric") or ""
            if metric in ("daily_active_addresses", "price_volatility_1d"):
                return self._fresh_series()
            return []  # funding/OI empty — must not lag-retry when fallback off

        with patch.object(client, "get_metric_timeseries", side_effect=fake_ts):
            res = client.fetch_features()
        # 4 lean + 2 leverage live fails = 6 (not 8 with research lag)
        self.assertEqual(calls["n"], 6)
        self.assertFalse(res.meta.get("leverage_fresh"))

    def test_asset_micro_one_call(self):
        from services.santiment_sidecar.client import SantimentClient

        client = SantimentClient("fake-key", inter_request_delay_sec=0)
        calls = {"n": 0}

        def fake_ts(**kwargs):
            calls["n"] += 1
            return self._fresh_series()

        with patch.object(client, "get_metric_timeseries", side_effect=fake_ts):
            out = client.fetch_asset_signals("ethereum", micro=True, try_research=False)
        self.assertEqual(out["meta"]["api_calls_this_fetch"], 1)
        self.assertEqual(calls["n"], 1)
        self.assertIn("daa", out["meta"]["metrics_ok"])


class TestSantimentSnapshot(unittest.TestCase):
    def test_meta_on_snapshot(self):
        snap = build_snapshot(
            {
                "btc_daa_delta_1d": 0.05,
                "eth_daa_delta_1d": 0.02,
                "btc_vol_1d": 0.02,
            },
            meta={
                "data_lag_days_max": 0.2,
                "metrics_ok": ["btc_daa", "eth_daa", "btc_vol_1d"],
                "metrics_failed": ["btc_social_volume"],
                "policy_inputs": ["daa", "vol"],
                "social_fresh": False,
                "lagged_excluded_from_policy": True,
            },
        )
        self.assertIn("meta", snap)
        self.assertEqual(snap["meta"]["data_lag_days_max"], 0.2)
        self.assertFalse(snap["meta"]["social_fresh"])
        self.assertEqual(snap["regime"], "NEUTRAL")
        self.assertIn("scores", snap)
        self.assertIn("onchain", snap["scores"])
        self.assertIn("composite", snap["scores"])

    def test_multi_score_pillars(self):
        d = decide_regime(
            {
                "btc_daa_delta_1d": -0.2,
                "eth_daa_delta_1d": -0.15,
                "btc_vol_1d": 0.03,
            }
        )
        self.assertIsNotNone(d.scores.get("onchain"))
        self.assertIsNone(d.scores.get("leverage"))
        self.assertIsNone(d.scores.get("social"))
        self.assertIn("onchain", d.scores.get("pillars") or [])
        self.assertGreater(d.confidence, 0.35)

    def test_leverage_only_when_fresh(self):
        feat = {
            "btc_daa_delta_1d": 0.05,
            "eth_daa_delta_1d": 0.02,
            "btc_vol_1d": 0.02,
            "btc_funding_rate": 0.001,
        }
        cold = decide_regime(feat, meta={"leverage_fresh": False})
        hot = decide_regime(feat, meta={"leverage_fresh": True})
        self.assertIsNone(cold.scores.get("leverage"))
        self.assertIsNotNone(hot.scores.get("leverage"))
        self.assertLess(hot.scores["leverage"], 0)  # positive funding → crowded


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
            {
                "btc_daa_delta_1d": -0.35,
                "eth_daa_delta_1d": -0.3,
                "btc_vol_1d": 0.03,
            },
            meta={"social_fresh": False, "policy_inputs": ["daa", "vol"], "metrics_ok": ["btc_daa"]},
        )
        with patch.dict(os.environ, {}, clear=False):
            result = process_santiment_ingest(snap, config_raw=cfg)
        self.assertTrue(result["ok"])
        stored = get_latest_snapshot(allow_redis=False)
        self.assertIsNotNone(stored)
        self.assertEqual(stored["regime"], "RISK_OFF")
        self.assertTrue(snapshot_is_fresh(stored))
        self.assertIn("meta", stored)

    def test_status_line_includes_lag(self):
        store_snapshot(
            {
                "source": "santiment",
                "regime": "NEUTRAL",
                "size_mult": 0.85,
                "ttl_sec": 1800,
                "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "meta": {
                    "data_lag_days_max": 0.1,
                    "metrics_ok": ["btc_daa", "btc_vol_1d"],
                    "metrics_failed": ["btc_social_volume"],
                },
            }
        )
        line = status_line()
        self.assertIn("lag=", line)
        self.assertIn("ok=2", line)
        self.assertIn("fail=1", line)


class TestSantimentPolicy(unittest.TestCase):
    def setUp(self):
        reset_for_tests()

    def tearDown(self):
        reset_for_tests()

    def test_fail_open_without_snapshot(self):
        with patch("services.santiment.policy.get_latest_snapshot", return_value=None):
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


class TestMarketPolicyFusion(unittest.TestCase):
    def setUp(self):
        reset_for_tests()

    def tearDown(self):
        reset_for_tests()

    def test_risk_off_soft_sentiment_not_defensive_threshold(self):
        """RISK_OFF sentiment must stay > -0.55 to avoid allocator DEFENSIVE dump."""
        store_snapshot(
            {
                "source": "santiment",
                "regime": "RISK_OFF",
                "size_mult": 0.35,
                "sensor_policy": "shadow",
                "ttl_sec": 1800,
            }
        )
        arch = {
            "santiment_risk_enabled": True,
            "market_oracle_risk_enabled": False,
        }
        bias = get_global_market_bias({"architecture": arch})
        self.assertTrue(bias["active"])
        self.assertGreater(bias["sentiment"], -0.55)
        self.assertEqual(bias["grid_spacing_mult"], 1.25)

    def test_mode_bias_momentum_to_hybrid_not_defensive(self):
        store_snapshot(
            {
                "source": "santiment",
                "regime": "RISK_OFF",
                "size_mult": 0.35,
                "sensor_policy": "shadow",
                "ttl_sec": 1800,
            }
        )
        arch = {
            "santiment_risk_enabled": True,
            "market_oracle_risk_enabled": False,
        }
        bias = get_global_market_bias({"architecture": arch})
        self.assertEqual(apply_global_mode_bias(MODE_MOMENTUM, bias), MODE_HYBRID)
        self.assertEqual(apply_global_mode_bias(MODE_GRID, bias), MODE_GRID)
        self.assertNotEqual(apply_global_mode_bias(MODE_MOMENTUM, bias), MODE_DEFENSIVE)

    def test_inject_sentiment(self):
        store_snapshot(
            {
                "source": "santiment",
                "regime": "RISK_ON",
                "size_mult": 0.9,
                "sensor_policy": "active",
                "ttl_sec": 1800,
            }
        )
        bias = get_global_market_bias(
            {
                "architecture": {
                    "santiment_risk_enabled": True,
                    "market_oracle_risk_enabled": False,
                }
            }
        )
        ctx = inject_global_sentiment({}, bias)
        self.assertIn("santiment_sentiment", ctx)
        self.assertGreater(ctx["santiment_sentiment"], 0)


class TestSantimentRoute(unittest.TestCase):
    def setUp(self):
        reset_for_tests()
        from aria_bot import app

        self.client = app.test_client()

    def tearDown(self):
        reset_for_tests()

    def test_ingest_route(self):
        snap = build_snapshot(
            {
                "btc_daa_delta_1d": 0.05,
                "eth_daa_delta_1d": 0.02,
                "btc_vol_1d": 0.02,
            },
            meta={"social_fresh": False, "metrics_ok": ["btc_daa"]},
        )
        with patch.dict(os.environ, {"SANTIMENT_INGEST_TOKEN": "t1"}, clear=False), \
             patch("services.santiment.ingest.santiment_ingest_enabled", return_value=True):
            resp = self.client.post(
                "/api/santiment/ingest",
                json=snap,
                headers={"X-Santiment-Token": "t1"},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json().get("ok"))


if __name__ == "__main__":
    unittest.main()
