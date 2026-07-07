import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from strategies.dca_sizing import compute_dca_usdt, resolve_dca_base_usdt


class TestDCASizing(unittest.TestCase):
    def test_high_score_larger_than_low_score(self):
        cfg = {"sizing": {"min_usdt": 80, "max_usdt": 400}}
        low = compute_dca_usdt(
            base_usdt=400,
            score=6,
            max_score=10,
            min_score=6,
            loss_pct=-8,
            round_index=0,
            max_rounds=3,
            dca_cfg=cfg,
        )
        high = compute_dca_usdt(
            base_usdt=400,
            score=10,
            max_score=10,
            min_score=6,
            loss_pct=-8,
            round_index=0,
            max_rounds=3,
            dca_cfg=cfg,
        )
        self.assertGreater(high, low)
        self.assertGreaterEqual(low, 80)
        self.assertLessEqual(high, 420)

    def test_recovery_smaller_than_accumulation(self):
        cfg = {"sizing": {"min_usdt": 50, "max_usdt": 400, "recovery_base_ratio": 0.35}}
        acc = compute_dca_usdt(
            base_usdt=400,
            score=8,
            max_score=10,
            min_score=6,
            loss_pct=-10,
            round_index=0,
            max_rounds=3,
            dca_cfg=cfg,
            is_recovery=False,
        )
        rec = compute_dca_usdt(
            base_usdt=400,
            score=8,
            max_score=10,
            min_score=6,
            loss_pct=-10,
            round_index=0,
            max_rounds=2,
            dca_cfg=cfg,
            is_recovery=True,
            recovery_ratio=0.35,
        )
        self.assertLess(rec, acc)

    def test_notional_ratio_raises_base_above_fixed_anchor(self):
        cfg = {
            "sizing": {
                "base_mode": "max",
                "notional_ratio": 0.30,
                "min_usdt": 300,
                "max_usdt": 1200,
                "min_multiplier": 0.55,
                "max_multiplier": 1.0,
            }
        }
        sized = compute_dca_usdt(
            base_usdt=500,
            score=8,
            max_score=10,
            min_score=6,
            loss_pct=-11,
            round_index=0,
            max_rounds=3,
            dca_cfg=cfg,
            position_notional_usdt=2760,
        )
        self.assertGreater(sized, 500)
        self.assertGreaterEqual(sized, 650)
        self.assertLessEqual(sized, 1200)

    def test_resolve_base_uses_max_of_fixed_and_notional(self):
        cfg = {
            "base_mode": "max",
            "notional_ratio": 0.30,
            "recovery_base_ratio": 0.35,
        }
        base = resolve_dca_base_usdt(
            base_usdt=500,
            position_notional_usdt=2760,
            cfg=cfg,
            is_recovery=False,
        )
        self.assertEqual(base, 828.0)


if __name__ == "__main__":
    unittest.main()