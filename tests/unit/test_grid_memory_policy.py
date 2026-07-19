"""Memory-aware grid sell policy (pure)."""

from __future__ import annotations

import unittest

from core.actions import HOLD, SELL_PARTIAL_20
from intelligence.memory.coin_facts import FactFlags
from strategies.grid_memory_policy import apply_grid_memory_sell_policy
from strategies.grid_plan import GridAction


def _sell(frac: float = 0.2) -> GridAction:
    return GridAction(
        action=SELL_PARTIAL_20,
        level_index=1,
        level_price=100.0,
        sell_pos_frac=frac,
        rationale="Grid sell L1",
    )


class TestGridMemoryPolicy(unittest.TestCase):
    def test_structure_risk_blocks_small_gain(self):
        flags = FactFlags(structure_risk=True, event_count=2)
        out = apply_grid_memory_sell_policy(
            _sell(),
            gain_pct=1.0,
            flags=flags,
            policy={"memory_structure_min_gain_pct": 2.0},
        )
        self.assertEqual(out.action, HOLD)
        self.assertIn("structure_risk", out.rationale)

    def test_structure_risk_allows_enough_gain(self):
        flags = FactFlags(structure_risk=True, event_count=2)
        out = apply_grid_memory_sell_policy(
            _sell(),
            gain_pct=5.0,
            flags=flags,
            policy={"memory_structure_min_gain_pct": 2.0},
        )
        self.assertEqual(out.action, SELL_PARTIAL_20)

    def test_hard_negative_blocks_grid_harvest(self):
        flags = FactFlags(hard_negative=True, event_count=1)
        out = apply_grid_memory_sell_policy(
            _sell(),
            gain_pct=2.0,
            flags=flags,
            policy={"memory_hard_neg_min_gain_pct": 5.0},
        )
        self.assertEqual(out.action, HOLD)
        self.assertIn("hard_negative", out.rationale)

    def test_momentum_holds_runner(self):
        flags = FactFlags(volume_breakout=True, event_count=1)
        out = apply_grid_memory_sell_policy(
            _sell(),
            gain_pct=4.0,
            flags=flags,
            policy={"memory_runner_hold_below_gain_pct": 8.0},
        )
        self.assertEqual(out.action, HOLD)
        self.assertIn("momentum", out.rationale)

    def test_no_flags_pass_through(self):
        out = apply_grid_memory_sell_policy(
            _sell(),
            gain_pct=-5.0,
            flags=FactFlags(),
            policy={},
        )
        self.assertEqual(out.action, SELL_PARTIAL_20)

    def test_memory_disabled(self):
        flags = FactFlags(hard_negative=True)
        out = apply_grid_memory_sell_policy(
            _sell(),
            gain_pct=0.0,
            flags=flags,
            policy={"memory_enabled": False},
        )
        self.assertEqual(out.action, SELL_PARTIAL_20)


if __name__ == "__main__":
    unittest.main()
