"""Unit tests for strategies.dca_fact_policy.apply_coin_fact_policy."""

from __future__ import annotations

from types import SimpleNamespace
from unittest import TestCase

from strategies.dca_fact_policy import apply_coin_fact_policy


class ApplyCoinFactPolicyTests(TestCase):
    def test_no_rule_passthrough(self):
        ctx = SimpleNamespace()
        mult, skip, reasons = apply_coin_fact_policy(
            ctx, {}, mult=1.0, skip=False, reasons=[]
        )
        self.assertEqual(mult, 1.0)
        self.assertFalse(skip)
        self.assertEqual(reasons, [])

    def test_caution_rule_reduces_multiplier(self):
        # fact_profit_taking default mult 0.7
        ctx = SimpleNamespace(fact_profit_taking=True)
        mult, skip, reasons = apply_coin_fact_policy(
            ctx, {}, mult=1.0, skip=False, reasons=[]
        )
        self.assertAlmostEqual(mult, 0.7)
        self.assertFalse(skip)
        self.assertIn("fact_profit_taking", reasons)

    def test_caution_uses_cfg_multiplier(self):
        ctx = SimpleNamespace(fact_structure_risk=True)
        mult, skip, reasons = apply_coin_fact_policy(
            ctx,
            {"fact_structure_risk_mult": 0.4},
            mult=1.0,
            skip=False,
            reasons=[],
        )
        self.assertAlmostEqual(mult, 0.4)
        self.assertFalse(skip)
        self.assertIn("fact_structure_risk", reasons)

    def test_boost_rule_increases_multiplier(self):
        # fact_volume_breakout default mult 1.1; not oversold-gated
        ctx = SimpleNamespace(fact_volume_breakout=True, loss_pct=0.0)
        mult, skip, reasons = apply_coin_fact_policy(
            ctx, {}, mult=1.0, skip=False, reasons=[]
        )
        self.assertAlmostEqual(mult, 1.1)
        self.assertFalse(skip)
        self.assertIn("fact_volume_breakout", reasons)

    def test_oversold_only_boost_requires_deep_loss(self):
        # fact_catalyst is oversold_only — needs loss_pct <= -5
        ctx_flat = SimpleNamespace(fact_catalyst=True, loss_pct=-2.0)
        mult, skip, reasons = apply_coin_fact_policy(
            ctx_flat, {}, mult=1.0, skip=False, reasons=[]
        )
        self.assertEqual(mult, 1.0)
        self.assertNotIn("fact_catalyst", reasons)

        ctx_os = SimpleNamespace(fact_catalyst=True, loss_pct=-6.0)
        mult, skip, reasons = apply_coin_fact_policy(
            ctx_os, {}, mult=1.0, skip=False, reasons=[]
        )
        self.assertAlmostEqual(mult, 1.1)
        self.assertIn("fact_catalyst", reasons)

    def test_hard_negative_skip(self):
        ctx = SimpleNamespace(fact_hard_negative=True)
        mult, skip, reasons = apply_coin_fact_policy(
            ctx, {}, mult=1.0, skip=False, reasons=[]
        )
        self.assertTrue(skip)
        self.assertEqual(mult, 1.0)
        self.assertIn("fact_hard_negative", reasons)

    def test_unlock_can_escalate_to_skip(self):
        # unlock mult 0.5 with high negative impact → fact_unlock_skip
        ctx = SimpleNamespace(fact_unlock=True, fact_min_impact=-0.9)
        mult, skip, reasons = apply_coin_fact_policy(
            ctx, {}, mult=1.0, skip=False, reasons=[]
        )
        self.assertTrue(skip)
        self.assertAlmostEqual(mult, 0.5)
        self.assertIn("fact_unlock", reasons)
        self.assertIn("fact_unlock_skip", reasons)

    def test_already_skip_short_circuits(self):
        ctx = SimpleNamespace(fact_volume_breakout=True, fact_profit_taking=True)
        mult, skip, reasons = apply_coin_fact_policy(
            ctx, {}, mult=1.0, skip=True, reasons=["prior"]
        )
        self.assertTrue(skip)
        self.assertEqual(mult, 1.0)
        self.assertEqual(reasons, ["prior"])

    def test_flow_only_blocks_boosts(self):
        ctx = SimpleNamespace(
            fact_flow_only=True,
            fact_volume_breakout=True,
            loss_pct=0.0,
        )
        mult, skip, reasons = apply_coin_fact_policy(
            ctx, {}, mult=1.0, skip=False, reasons=[]
        )
        # caution applies (0.8), boosts suppressed by flow_only
        self.assertAlmostEqual(mult, 0.8)
        self.assertIn("fact_flow_only", reasons)
        self.assertNotIn("fact_volume_breakout", reasons)
