"""#330 slice 3: stop mirroring grid_states into config.json."""

from __future__ import annotations

import inspect
import os
import sys
import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from storage.grid_plan_store import (
    load_grid_plan,
    migrate_legacy_grid_states_once,
    save_grid_plan,
)


LEGACY_KEY = "BTC/USDT_4h"
LEGACY_PLAN = {
    "symbol": "BTC/USDT",
    "timeframe": "4h",
    "center_price": 100000.0,
    "spacing": 500.0,
    "levels": [],
}


def _fake_mongo():
    store: dict = {}
    fake_db = MagicMock()

    def replace_one(filt, doc, upsert=False):
        store[doc["_id"]] = doc
        return MagicMock()

    def find_one(filt):
        return store.get(filt.get("_id"))

    coll = MagicMock()
    coll.replace_one.side_effect = replace_one
    coll.find_one.side_effect = find_one
    fake_db.__getitem__ = lambda self, name: coll
    return fake_db, store, coll


class TestSaveGridPlanNoConfigMirror(unittest.TestCase):
    def test_also_legacy_config_defaults_false(self):
        params = inspect.signature(save_grid_plan).parameters
        self.assertFalse(params["also_legacy_config"].default)

    def test_save_grid_plan_does_not_call_save_config(self):
        fake_db, _store, _coll = _fake_mongo()
        save_spy = MagicMock(return_value=True)

        with patch("storage.mongo_client.get_database", return_value=fake_db), patch(
            "storage.mongo_client.assert_safe_dev_db_mutation"
        ), patch(
            "storage.mongo_client.resolve_database_name", return_value="xagent_test"
        ), patch(
            "core.tenant_context.resolve_tenant_id", return_value="default"
        ), patch(
            "core.tenant_context.resolve_tenant_scope", return_value="demo"
        ), patch(
            "data_manager.get_config", return_value={}
        ), patch(
            "data_manager.save_config", save_spy
        ):
            self.assertTrue(
                save_grid_plan("BTC/USDT", "4h", dict(LEGACY_PLAN), test=True)
            )
            loaded = load_grid_plan("BTC/USDT", "4h", test=True)

        save_spy.assert_not_called()
        self.assertIsNotNone(loaded)
        self.assertAlmostEqual(float(loaded["center_price"]), 100000.0)


class TestMigrateLegacyGridStates(unittest.TestCase):
    def test_migrate_copies_then_second_call_is_noop(self):
        fake_db, mongo_store, _coll = _fake_mongo()
        cfg_holder = {
            "grid_states": {LEGACY_KEY: dict(LEGACY_PLAN)},
            "keep_me": True,
        }
        save_calls: list[dict] = []

        def fake_get_config(*_a, **_k):
            return dict(cfg_holder)

        def fake_save_config(new_cfg, tenant_id=None):
            save_calls.append(dict(new_cfg))
            cfg_holder.clear()
            cfg_holder.update(new_cfg)
            return True

        with ExitStack() as stack:
            stack.enter_context(patch("storage.mongo_client.get_database", return_value=fake_db))
            stack.enter_context(patch("storage.mongo_client.assert_safe_dev_db_mutation"))
            stack.enter_context(
                patch("storage.mongo_client.resolve_database_name", return_value="xagent_test")
            )
            stack.enter_context(patch("core.tenant_context.resolve_tenant_id", return_value="default"))
            stack.enter_context(patch("core.tenant_context.resolve_tenant_scope", return_value="demo"))
            stack.enter_context(patch("data_manager.get_config", side_effect=fake_get_config))
            stack.enter_context(patch("data_manager.save_config", side_effect=fake_save_config))
            first = migrate_legacy_grid_states_once(test=True)
            loaded = load_grid_plan("BTC/USDT", "4h", test=True)
            second = migrate_legacy_grid_states_once(test=True)

        self.assertTrue(first["ok"])
        self.assertTrue(first["stripped"])
        self.assertIn(LEGACY_KEY, first["migrated_keys"])
        self.assertIsNotNone(loaded)
        self.assertAlmostEqual(float(loaded["center_price"]), 100000.0)
        self.assertNotIn("grid_states", cfg_holder)
        self.assertTrue(cfg_holder.get("keep_me"))
        self.assertEqual(len(save_calls), 1)
        self.assertNotIn("grid_states", save_calls[0])

        self.assertTrue(second["ok"])
        self.assertTrue(second["skipped"])
        self.assertFalse(second["stripped"])
        self.assertEqual(len(save_calls), 1)
        self.assertTrue(mongo_store)

    def test_failed_mongo_upsert_leaves_config_grid_states(self):
        cfg_holder = {
            "grid_states": {LEGACY_KEY: dict(LEGACY_PLAN)},
            "keep_me": True,
        }
        save_spy = MagicMock(return_value=True)

        def fake_get_config(*_a, **_k):
            return dict(cfg_holder)

        with patch(
            "storage.grid_plan_store.load_grid_plans_document",
            return_value={"plans": {}},
        ), patch(
            "storage.grid_plan_store.save_grid_plans_document",
            return_value=False,
        ), patch(
            "data_manager.get_config", side_effect=fake_get_config
        ), patch(
            "data_manager.save_config", save_spy
        ):
            result = migrate_legacy_grid_states_once(test=True)

        self.assertFalse(result["ok"])
        self.assertFalse(result["stripped"])
        save_spy.assert_not_called()
        self.assertIn("grid_states", cfg_holder)
        self.assertIn(LEGACY_KEY, cfg_holder["grid_states"])

    def test_failed_mongo_load_leaves_config_grid_states(self):
        from storage.errors import LedgerUnavailable

        cfg_holder = {"grid_states": {LEGACY_KEY: dict(LEGACY_PLAN)}}
        save_spy = MagicMock(return_value=True)

        with patch(
            "storage.grid_plan_store.load_grid_plans_document",
            side_effect=LedgerUnavailable(op="load_grid_plans_document"),
        ), patch(
            "data_manager.get_config", return_value=dict(cfg_holder)
        ), patch(
            "data_manager.save_config", save_spy
        ):
            result = migrate_legacy_grid_states_once(test=True)

        self.assertFalse(result["ok"])
        self.assertFalse(result["stripped"])
        save_spy.assert_not_called()
        self.assertIn("grid_states", cfg_holder)


if __name__ == "__main__":
    unittest.main()
