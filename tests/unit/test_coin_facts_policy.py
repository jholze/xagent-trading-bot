"""#103 coin-fact policy factors on shipped evaluate_dca_policy."""

from __future__ import annotations

import unittest

from strategies.dca_policy import DcaContext, dca_policy_config, evaluate_dca_policy


def _cfg(**overrides):
    base = {
        "enabled": True,
        "shadow": False,
        "harvest_mode": "soft",
        "deploy_mult": 1.0,  # isolate fact mults
        "size_mult_deploy": 99,  # avoid deploy boost
        "size_mult_harvest": 0.1,
    }
    base.update(overrides)
    return dca_policy_config({"policy": base})


class TestCoinFactPolicy(unittest.TestCase):
    def test_hard_negative_skips(self):
        ctx = DcaContext(
            symbol="ALLO/USDT",
            cash_mode="STEADY",
            fusion_size_mult=1.0,
            fact_hard_negative=True,
        )
        r = evaluate_dca_policy(ctx, _cfg())
        self.assertTrue(r.skip)
        self.assertIn("fact_hard_negative", r.reason_codes)

    def test_no_facts_unchanged_vs_baseline(self):
        base = DcaContext(symbol="ALLO/USDT", cash_mode="STEADY", fusion_size_mult=1.0)
        with_facts = DcaContext(
            symbol="ALLO/USDT",
            cash_mode="STEADY",
            fusion_size_mult=1.0,
            fact_event_count=0,
        )
        rb = evaluate_dca_policy(base, _cfg())
        rf = evaluate_dca_policy(with_facts, _cfg())
        self.assertEqual(rb.skip, rf.skip)
        self.assertAlmostEqual(rb.size_mult, rf.size_mult, places=6)
        self.assertFalse(any(c.startswith("fact_") for c in rf.reason_codes))

    def test_profit_taking_reduces_mult(self):
        ctx = DcaContext(
            symbol="ALLO/USDT",
            cash_mode="STEADY",
            fusion_size_mult=1.0,
            fact_profit_taking=True,
        )
        r = evaluate_dca_policy(ctx, _cfg())
        self.assertFalse(r.skip)
        self.assertLessEqual(r.size_mult, 0.7 + 1e-9)
        self.assertIn("fact_profit_taking", r.reason_codes)

    def test_unlock_reduces_or_skips(self):
        ctx = DcaContext(
            symbol="ALLO/USDT",
            cash_mode="STEADY",
            fusion_size_mult=1.0,
            fact_unlock=True,
            fact_min_impact=-0.9,
        )
        r = evaluate_dca_policy(ctx, _cfg())
        self.assertIn("fact_unlock", r.reason_codes)
        self.assertTrue(r.skip or r.size_mult <= 0.5 + 1e-9)

    def test_volume_breakout_boosts(self):
        ctx = DcaContext(
            symbol="ALLO/USDT",
            cash_mode="STEADY",
            fusion_size_mult=1.0,
            fact_volume_breakout=True,
        )
        r = evaluate_dca_policy(ctx, _cfg())
        self.assertFalse(r.skip)
        self.assertGreaterEqual(r.size_mult, 1.1 - 1e-9)
        self.assertIn("fact_volume_breakout", r.reason_codes)

    def test_noise_only_no_boost(self):
        ctx = DcaContext(
            symbol="ALLO/USDT",
            cash_mode="STEADY",
            fusion_size_mult=1.0,
            fact_noise_only=True,
        )
        r = evaluate_dca_policy(ctx, _cfg())
        self.assertAlmostEqual(r.size_mult, 1.0, places=4)
        self.assertIn("fact_noise_ignore", r.reason_codes)

    def test_hard_neg_beats_utility(self):
        ctx = DcaContext(
            symbol="ALLO/USDT",
            cash_mode="STEADY",
            fusion_size_mult=1.0,
            loss_pct=-10.0,
            fact_hard_negative=True,
            fact_utility=True,
            fact_volume_breakout=True,
        )
        r = evaluate_dca_policy(ctx, _cfg())
        self.assertTrue(r.skip)
        self.assertIn("fact_hard_negative", r.reason_codes)
        self.assertNotIn("fact_utility", r.reason_codes)


if __name__ == "__main__":
    unittest.main()
