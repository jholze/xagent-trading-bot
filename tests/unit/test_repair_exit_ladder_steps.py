import unittest

from strategies.exit_ladder import reconcile_exit_ladder_step


class TestRepairExitLadderPlan(unittest.TestCase):
    def test_plan_detects_step_zero_with_sold_percent(self):
        from scripts.repair_exit_ladder_steps import plan_repairs

        positions = {
            "VELVET_USDT_4h": {
                "amount": 400.0,
                "peak_amount": 1000.0,
                "sold_percent": 0.6,
                "exit_ladder_step": 0,
                "average_entry": 1.0,
                "realized_pnl": 100.0,
            }
        }
        rows = plan_repairs(positions, tiers=[0.35, 0.35, 0.3])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["step_before"], 0)
        self.assertGreater(rows[0]["step_after"], 0)
        self.assertEqual(
            rows[0]["fingerprint_before"],
            rows[0]["fingerprint_after"],
        )

    def test_plan_skips_already_synced(self):
        from scripts.repair_exit_ladder_steps import plan_repairs

        pos = {
            "amount": 400.0,
            "peak_amount": 1000.0,
            "sold_percent": 0.6,
            "exit_ladder_step": 2,
            "average_entry": 1.0,
            "realized_pnl": 0.0,
        }
        reconcile_exit_ladder_step(pos, [0.35, 0.35, 0.3])
        rows = plan_repairs({"X_USDT_4h": pos}, tiers=[0.35, 0.35, 0.3])
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()