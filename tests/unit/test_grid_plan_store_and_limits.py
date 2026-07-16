"""Phase B rest (mongo grid plans) + Phase C (limit shadow book)."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from strategies.grid_limits import (
    enforce_fee_spacing,
    fee_aware_min_spacing,
    mark_plan_level_filled,
    plan_to_limit_specs,
    simulate_limit_grid_path,
)
from strategies.grid_plan import build_grid_plan
from storage.grid_plan_store import load_grid_plan, save_grid_plan


class TestGridPlanStore(unittest.TestCase):
    def test_save_load_roundtrip_mongo(self):
        plan = build_grid_plan("T/USDT", "4h", 50.0, atr_pct=2.0)
        payload = plan.to_dict()
        payload["center_price"] = plan.center

        fake_db = MagicMock()
        store: dict = {}

        def replace_one(filt, doc, upsert=False):
            store[doc["_id"]] = doc
            return MagicMock()

        def find_one(filt):
            return store.get(filt.get("_id"))

        coll = MagicMock()
        coll.replace_one.side_effect = replace_one
        coll.find_one.side_effect = find_one
        fake_db.__getitem__ = lambda self, name: coll

        with patch("storage.mongo_client.get_database", return_value=fake_db), patch(
            "storage.mongo_client.assert_safe_dev_db_mutation"
        ), patch(
            "storage.mongo_client.resolve_database_name", return_value="xagent_test"
        ), patch(
            "data_manager.get_config", return_value={}
        ), patch(
            "data_manager.save_config", return_value=True
        ), patch(
            "core.tenant_context.resolve_tenant_id", return_value="henry"
        ), patch(
            "core.tenant_context.resolve_tenant_scope", return_value="demo"
        ):
            self.assertTrue(save_grid_plan("T/USDT", "4h", payload, test=True))
            loaded = load_grid_plan("T/USDT", "4h", test=True)
        self.assertIsNotNone(loaded)
        self.assertAlmostEqual(float(loaded["center"]), 50.0, places=4)
        self.assertTrue(loaded.get("levels"))


class TestGridLimits(unittest.TestCase):
    def test_fee_aware_min_spacing(self):
        m = fee_aware_min_spacing(100.0, fee_pct=0.1, safety_mult=3.0)
        # 2 * 0.001 * 3 * 100 = 0.6
        self.assertAlmostEqual(m, 0.6, places=5)

    def test_enforce_fee_widens_tight_plan(self):
        plan = build_grid_plan("T/USDT", "4h", 100.0, atr_pct=0.01, spacing_atr_mult=0.1)
        wide = enforce_fee_spacing(plan, fee_pct=0.2)
        self.assertGreaterEqual(wide.spacing, fee_aware_min_spacing(100.0, fee_pct=0.2))

    def test_plan_to_limit_specs(self):
        plan = build_grid_plan("T/USDT", "4h", 100.0, n_buy_levels=2, n_sell_levels=2)
        buys = plan_to_limit_specs(plan, has_position=False, base_buy_usdt=400)
        self.assertTrue(all(s.side == "buy" for s in buys))
        self.assertEqual(len(buys), 2)
        both = plan_to_limit_specs(
            plan, has_position=True, position_amount=10.0, base_buy_usdt=400,
        )
        self.assertEqual(len(both), 4)

    def test_mark_level_filled(self):
        plan = build_grid_plan("T/USDT", "4h", 100.0, n_buy_levels=1, n_sell_levels=1)
        cid = "grid:T/USDT:4h:buy:L1"
        self.assertTrue(mark_plan_level_filled(plan, cid))
        self.assertTrue(any(lv.filled for lv in plan.levels if lv.side == "buy"))

    def test_limit_shadow_simulation_runs(self):
        import math

        prices = [100 + 6 * math.sin(i / 6.0) for i in range(100)]
        res = simulate_limit_grid_path(prices, initial_cash=10_000, base_buy_usdt=400)
        self.assertEqual(res.get("mode"), "limit_shadow")
        self.assertIn("final_equity", res)
        self.assertGreaterEqual(res["trades"], 0)


if __name__ == "__main__":
    unittest.main()
