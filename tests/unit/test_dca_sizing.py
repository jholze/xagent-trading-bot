import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from strategies.dca_sizing import compute_dca_usdt


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


if __name__ == "__main__":
    unittest.main()