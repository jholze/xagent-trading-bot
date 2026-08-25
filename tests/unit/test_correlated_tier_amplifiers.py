"""Unit tests for correlated-tier regime/news amplifiers (pure, no network)."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from services.correlated_tier.amplifiers import apply_amplifiers


def _payload(*, confirming: int = 1, min_confirming: int = 2, active: bool | None = None) -> dict:
    if active is None:
        active = confirming >= min_confirming
    return {
        "group": "us_stock",
        "per_symbol": {"AAA/USDT": confirming >= 1, "BBB/USDT": confirming >= 2},
        "active": active,
        "confirming": confirming,
        "min_confirming": min_confirming,
        "drawdown_pct": 5.0,
        "window_sec": 600.0,
        "updated_at": 1_700_000_000.0,
    }


class TestApplyAmplifiersOff(unittest.TestCase):
    def test_both_amplifiers_off_returns_payload_unchanged(self):
        payload = _payload(confirming=1, min_confirming=2)
        snapshot = dict(payload)
        ct_cfg = {
            "regime_amplify_enabled": False,
            "news_pulse_enabled": False,
        }
        out = apply_amplifiers(payload, {}, ct_cfg)
        self.assertIs(out, payload)
        self.assertEqual(out, snapshot)
        self.assertNotIn("amplifier_delta", out)
        self.assertNotIn("regime_amplified", out)
        self.assertNotIn("news_amplified", out)


class TestApplyAmplifiersRegime(unittest.TestCase):
    def test_regime_amplifier_alone_lowers_bar_by_exactly_1(self):
        payload = _payload(confirming=1, min_confirming=2)
        ct_cfg = {
            "regime_amplify_enabled": True,
            "news_pulse_enabled": False,
            "regime_amplify_regimes": ["RISK_OFF", "CRASH"],
        }
        with patch(
            "services.correlated_tier.amplifiers.get_global_market_bias",
            return_value={"regime": "RISK_OFF"},
        ):
            out = apply_amplifiers(payload, {}, ct_cfg)
        self.assertEqual(out["amplifier_delta"], 1)
        self.assertTrue(out["regime_amplified"])
        self.assertFalse(out["news_amplified"])
        self.assertEqual(out["min_confirming"], 2)
        self.assertEqual(out["confirming"], 1)
        self.assertTrue(out["active"])
        self.assertEqual(out["group"], "us_stock")

    def test_regime_not_in_amplify_set_is_noop_delta(self):
        payload = _payload(confirming=1, min_confirming=2)
        ct_cfg = {
            "regime_amplify_enabled": True,
            "news_pulse_enabled": False,
            "regime_amplify_regimes": ["RISK_OFF", "CRASH"],
        }
        with patch(
            "services.correlated_tier.amplifiers.get_global_market_bias",
            return_value={"regime": "RISK_ON"},
        ):
            out = apply_amplifiers(payload, {}, ct_cfg)
        self.assertEqual(out["amplifier_delta"], 0)
        self.assertFalse(out["regime_amplified"])
        self.assertFalse(out["active"])

    def test_regime_call_error_fails_open(self):
        payload = _payload(confirming=1, min_confirming=2)
        ct_cfg = {"regime_amplify_enabled": True, "news_pulse_enabled": False}
        with patch(
            "services.correlated_tier.amplifiers.get_global_market_bias",
            side_effect=RuntimeError("redis down"),
        ):
            out = apply_amplifiers(payload, {}, ct_cfg)
        self.assertEqual(out["amplifier_delta"], 0)
        self.assertFalse(out["regime_amplified"])
        self.assertFalse(out["active"])


class TestApplyAmplifiersNews(unittest.TestCase):
    def test_news_amplifier_alone_lowers_bar_by_exactly_1(self):
        payload = _payload(confirming=1, min_confirming=2)
        ct_cfg = {
            "regime_amplify_enabled": False,
            "news_pulse_enabled": True,
            "news_pulse_bearish_threshold": 0.55,
            "news_pulse_min_confidence": 0.34,
        }
        with patch(
            "services.correlated_tier.amplifiers.get_cached_market_pulse",
            return_value={"bearish_score": 0.8, "confidence": 0.5, "event_count": 4, "top_events": []},
        ):
            out = apply_amplifiers(payload, {}, ct_cfg)
        self.assertEqual(out["amplifier_delta"], 1)
        self.assertTrue(out["news_amplified"])
        self.assertFalse(out["regime_amplified"])
        self.assertTrue(out["active"])

    def test_news_below_threshold_does_not_amplify(self):
        payload = _payload(confirming=1, min_confirming=2)
        ct_cfg = {
            "regime_amplify_enabled": False,
            "news_pulse_enabled": True,
            "news_pulse_bearish_threshold": 0.55,
            "news_pulse_min_confidence": 0.34,
        }
        with patch(
            "services.correlated_tier.amplifiers.get_cached_market_pulse",
            return_value={"bearish_score": 0.2, "confidence": 0.9, "event_count": 8, "top_events": []},
        ):
            out = apply_amplifiers(payload, {}, ct_cfg)
        self.assertEqual(out["amplifier_delta"], 0)
        self.assertFalse(out["news_amplified"])
        self.assertFalse(out["active"])


class TestApplyAmplifiersCombined(unittest.TestCase):
    def test_both_together_capped_at_default_max_combined_delta_1(self):
        payload = _payload(confirming=1, min_confirming=3)
        ct_cfg = {
            "regime_amplify_enabled": True,
            "news_pulse_enabled": True,
            "regime_amplify_regimes": ["RISK_OFF", "CRASH"],
        }
        with patch(
            "services.correlated_tier.amplifiers.get_global_market_bias",
            return_value={"regime": "CRASH"},
        ), patch(
            "services.correlated_tier.amplifiers.get_cached_market_pulse",
            return_value={"bearish_score": 0.9, "confidence": 1.0, "event_count": 8, "top_events": []},
        ):
            out = apply_amplifiers(payload, {}, ct_cfg)
        self.assertEqual(out["amplifier_delta"], 1)
        self.assertTrue(out["regime_amplified"])
        self.assertTrue(out["news_amplified"])
        # min 3 - 1 = 2; confirming 1 → still not active
        self.assertFalse(out["active"])

    def test_raised_cap_allows_combined_delta_of_2(self):
        payload = _payload(confirming=1, min_confirming=3)
        ct_cfg = {
            "regime_amplify_enabled": True,
            "news_pulse_enabled": True,
            "regime_amplify_regimes": ["RISK_OFF", "CRASH"],
            "news_pulse_max_combined_delta": 2,
        }
        with patch(
            "services.correlated_tier.amplifiers.get_global_market_bias",
            return_value={"regime": "CRASH"},
        ), patch(
            "services.correlated_tier.amplifiers.get_cached_market_pulse",
            return_value={"bearish_score": 0.9, "confidence": 1.0, "event_count": 8, "top_events": []},
        ):
            out = apply_amplifiers(payload, {}, ct_cfg)
        self.assertEqual(out["amplifier_delta"], 2)
        self.assertTrue(out["active"])

    def test_effective_min_confirming_never_below_1(self):
        payload = _payload(confirming=0, min_confirming=1)
        ct_cfg = {
            "regime_amplify_enabled": True,
            "news_pulse_enabled": True,
            "regime_amplify_regimes": ["RISK_OFF", "CRASH"],
            "news_pulse_max_combined_delta": 2,
        }
        with patch(
            "services.correlated_tier.amplifiers.get_global_market_bias",
            return_value={"regime": "CRASH"},
        ), patch(
            "services.correlated_tier.amplifiers.get_cached_market_pulse",
            return_value={"bearish_score": 0.99, "confidence": 1.0, "event_count": 10, "top_events": []},
        ):
            out = apply_amplifiers(payload, {}, ct_cfg)
        # amplifiers cannot invent a confirming proxy
        self.assertFalse(out["active"])
        self.assertEqual(out["confirming"], 0)
        self.assertEqual(out["min_confirming"], 1)
        self.assertGreaterEqual(out["amplifier_delta"], 1)


if __name__ == "__main__":
    unittest.main()
