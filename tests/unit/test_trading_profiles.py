"""Unit tests for trading profile presets and config merge."""

from __future__ import annotations

import copy
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.trading_profiles import (
    DEFAULT_PROFILE,
    TRADING_PROFILE_PRESETS,
    apply_effective_config,
    build_tenant_seed_config,
    coin_filters_config,
    deep_merge_dicts,
    resolve_profile_name,
)
from core.tenant_context import tenant_context
from data_manager import get_config, load_config, save_config
from storage.mongo_client import TEST_DB_NAME, drop_database


class TestTradingProfileMerge(unittest.TestCase):
    def test_deep_merge_nested_coin_filters(self):
        base = {"coin_filters": {"min_market_cap_usd": 1, "block_sources": []}}
        overlay = {"coin_filters": {"max_atr_pct": 5.0}}
        merged = deep_merge_dicts(base, overlay)
        self.assertEqual(merged["coin_filters"]["min_market_cap_usd"], 1)
        self.assertEqual(merged["coin_filters"]["max_atr_pct"], 5.0)
        self.assertEqual(merged["coin_filters"]["block_sources"], [])

    def test_conservative_preset_risk(self):
        base = {"max_open_positions": 99, "max_usdt_per_trade": 999}
        merged = apply_effective_config(base, {"trading_profile": "conservative"})
        preset = TRADING_PROFILE_PRESETS["conservative"]
        self.assertEqual(merged["max_open_positions"], preset["max_open_positions"])
        self.assertEqual(merged["max_usdt_per_trade"], preset["max_usdt_per_trade"])
        self.assertEqual(merged["coin_filters"]["max_atr_pct"], 4.0)
        self.assertFalse(merged["coin_filters"]["allow_trending_watchlist"])

    def test_tenant_override_wins_over_preset(self):
        base = {"max_open_positions": 99}
        merged = apply_effective_config(
            base,
            {"trading_profile": "conservative", "max_open_positions": 2},
        )
        self.assertEqual(merged["max_open_positions"], 2)
        self.assertEqual(merged["trading_profile"], "conservative")

    def test_no_profile_without_explicit_name(self):
        base = {"max_open_positions": 7}
        merged = apply_effective_config(base, None)
        self.assertEqual(merged["max_open_positions"], 7)
        self.assertNotIn("coin_filters", merged)

    def test_resolve_profile_from_base_config(self):
        base = {"trading_profile": "aggressive"}
        self.assertEqual(resolve_profile_name(base, None), "aggressive")
        self.assertEqual(
            resolve_profile_name(base, {"trading_profile": "conservative"}),
            "conservative",
        )

    def test_build_tenant_seed_balanced(self):
        seed = build_tenant_seed_config("balanced")
        self.assertEqual(seed["trading_profile"], DEFAULT_PROFILE)
        self.assertEqual(seed["trading_mode"], "paper")
        self.assertEqual(seed["max_open_positions"], TRADING_PROFILE_PRESETS["balanced"]["max_open_positions"])

    def test_coin_filters_config_defaults(self):
        cfg = coin_filters_config({})
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["min_market_cap_usd"], 5_000_000)


class TestTradingProfileTenantLoad(unittest.TestCase):
    def setUp(self):
        os.environ["PYTEST_RUNNING"] = "1"
        os.environ["MONGODB_DB"] = TEST_DB_NAME
        drop_database(test=True)
        self.base = {
            "virtual_trading": True,
            "max_open_positions": 99,
            "architecture": {"ledger_backend": "mongo"},
        }

    def tearDown(self):
        drop_database(test=True)

    def test_tenant_load_merges_profile_and_overrides(self):
        with patch("data_manager._load_default_config_from_disk", return_value=copy.deepcopy(self.base)):
            with patch("data_manager._should_use_mongo_for_tenant_config", return_value=True):
                with tenant_context("tp_merge", scope="paper"):
                    save_config({"trading_profile": "conservative", "max_open_positions": 3})
                    cfg = get_config()
        self.assertEqual(cfg.get("trading_profile"), "conservative")
        self.assertEqual(cfg.get("max_open_positions"), 3)
        self.assertEqual(cfg["coin_filters"]["max_atr_pct"], 4.0)
        self.assertEqual(cfg.get("virtual_trading"), True)

    def test_tenant_without_body_uses_base_only(self):
        with patch("data_manager._load_default_config_from_disk", return_value=copy.deepcopy(self.base)):
            with patch("data_manager._should_use_mongo_for_tenant_config", return_value=True):
                cfg = load_config(tenant_id="tp_empty")
        self.assertEqual(cfg.get("max_open_positions"), 99)


if __name__ == "__main__":
    unittest.main()