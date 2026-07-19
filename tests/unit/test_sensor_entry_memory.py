"""Sensor entry memory gates (pure)."""

from __future__ import annotations

import unittest

from intelligence.memory.coin_facts import FactFlags
from strategies.entry_sensor_15m import evaluate_entry_sensor_15m
from strategies.sensor_entry_memory import apply_sensor_memory_entry_policy


_METRICS = {
    "volume_spike_ratio": 4.0,
    "body_atr_ratio": 0.5,
    "price_momentum": True,
}

_CFG = {
    "enabled": True,
    "mode": "active",
    "vol_spike_mult": 3.0,
    "fakeout_min_body_atr_ratio": 0.3,
    "block_buy_if_rsi_4h_above": 75,
    "require_ema_breakout": False,
    "memory_enabled": True,
    "memory_block_structure_risk": True,
    "memory_block_hard_negative": True,
    "memory_honor_soft_block": True,
}


class TestSensorEntryMemory(unittest.TestCase):
    def test_structure_risk_blocks(self):
        v = apply_sensor_memory_entry_policy(
            flags=FactFlags(structure_risk=True, event_count=2),
            cfg=_CFG,
        )
        self.assertFalse(v.allow)
        self.assertIn("structure_risk", v.reason)

    def test_hard_negative_blocks(self):
        v = apply_sensor_memory_entry_policy(
            flags=FactFlags(hard_negative=True),
            cfg=_CFG,
        )
        self.assertFalse(v.allow)

    def test_soft_block_profile(self):
        v = apply_sensor_memory_entry_policy(
            flags=None,
            entry_bias="soft_block",
            cfg=_CFG,
        )
        self.assertFalse(v.allow)
        self.assertIn("soft_block", v.reason)

    def test_flow_only_size_down(self):
        v = apply_sensor_memory_entry_policy(
            flags=FactFlags(flow_only=True),
            cfg=_CFG,
        )
        self.assertTrue(v.allow)
        self.assertLess(v.size_mult, 1.0)

    def test_evaluate_integrates_memory_block(self):
        r = evaluate_entry_sensor_15m(
            watched=True,
            metrics=_METRICS,
            cfg=_CFG,
            rsi_4h=50.0,
            memory_flags=FactFlags(structure_risk=True, event_count=1),
        )
        self.assertFalse(r.triggered)
        self.assertIn("structure_risk", r.rationale)

    def test_evaluate_passes_clean(self):
        r = evaluate_entry_sensor_15m(
            watched=True,
            metrics=_METRICS,
            cfg=_CFG,
            rsi_4h=50.0,
            memory_flags=FactFlags(),
            memory_entry_bias="neutral",
        )
        self.assertTrue(r.triggered)
        self.assertAlmostEqual(r.size_mult, 1.0)


if __name__ == "__main__":
    unittest.main()
