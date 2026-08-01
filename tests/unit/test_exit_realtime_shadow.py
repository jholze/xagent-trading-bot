"""Unit tests for exit_realtime shadow evaluation (no live Gate)."""

from __future__ import annotations

import unittest

from services.exit_realtime.config import (
    exit_realtime_enabled,
    exit_realtime_mode,
    exit_realtime_sources,
)
from services.exit_realtime.shadow_eval import (
    evaluate_would_sells,
    from_gate_pair,
    to_gate_pair,
)


class TestExitRealtimeConfig(unittest.TestCase):
    def test_defaults_off(self):
        self.assertFalse(exit_realtime_enabled({}))
        self.assertEqual(exit_realtime_mode({}), "shadow")

    def test_enabled_shadow(self):
        raw = {"exit_realtime": {"enabled": True, "mode": "shadow"}}
        self.assertTrue(exit_realtime_enabled(raw))
        self.assertEqual(exit_realtime_mode(raw), "shadow")

    def test_sources(self):
        raw = {
            "exit_realtime": {
                "enabled": True,
                "sources": ["trailing_take_profit"],
            }
        }
        self.assertEqual(exit_realtime_sources(raw), frozenset({"trailing_take_profit"}))


class TestShadowEval(unittest.TestCase):
    def test_gate_pair_roundtrip(self):
        self.assertEqual(to_gate_pair("TAG/USDT"), "TAG_USDT")
        self.assertEqual(from_gate_pair("TAG_USDT"), "TAG/USDT")

    def test_no_sell_when_flat_gain(self):
        pos = {
            "average_entry": 1.0,
            "recent_high": 1.02,
            "amount": 10,
        }
        params = {
            "trailing_stop": {
                "enabled": True,
                "mode": "live",
                "activation_gain_pct": 5,
                "min_trail_pct": 8,
                "max_trail_pct": 25,
                "atr_multiplier": 2.0,
            },
            "trailing_take_profit": {
                "enabled": True,
                "mode": "live",
                "arm_gain_pct": 12,
                "min_gain_pct": 10,
                "min_gain_pct_floor": 8,
                "trail_pct": 6,
                "dynamic_trail": True,
                "max_steps": 1,
            },
        }
        events = evaluate_would_sells(
            symbol="TAG/USDT",
            timeframe="1h",
            price=1.04,
            position=pos,
            strategy_params=params,
            atr_pct=3.0,
        )
        self.assertEqual(events, [])

    def test_trailing_stop_would_fire(self):
        # Peak high, deep drop, gain still above activation
        pos = {
            "average_entry": 1.0,
            "recent_high": 1.20,
            "amount": 10,
        }
        params = {
            "trailing_stop": {
                "enabled": True,
                "mode": "live",
                "activation_gain_pct": 5,
                "min_trail_pct": 8,
                "max_trail_pct": 25,
                "atr_multiplier": 2.0,
            },
            "trailing_take_profit": {
                "enabled": True,
                "mode": "live",
                "arm_gain_pct": 50,
                "min_gain_pct": 40,
                "trail_pct": 6,
                "dynamic_trail": False,
                "max_steps": 1,
            },
        }
        # price 1.08 = +8% gain, drop from 1.20 = 10% > trail 8%
        events = evaluate_would_sells(
            symbol="H/USDT",
            timeframe="1h",
            price=1.08,
            position=pos,
            strategy_params=params,
            atr_pct=3.0,
            sources=frozenset({"trailing_stop", "trailing_take_profit"}),
        )
        sources = {e.get("source") for e in events}
        self.assertIn("trailing_stop", sources)
        self.assertTrue(all(e.get("type") == "exit_ws_shadow" for e in events))


if __name__ == "__main__":
    unittest.main()
