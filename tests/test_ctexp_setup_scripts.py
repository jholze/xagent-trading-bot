#!/usr/bin/env python3
"""Cheap tests for ctexp setup/patch helpers — no Mongo writes.

Confirms the Henry-style _deep_merge preserves nested correlated_tier.groups
when the experiment overlay only flips enabled + rotation knobs.

  python3.13 -m pytest tests/test_ctexp_setup_scripts.py -v
  python3.13 tests/test_ctexp_setup_scripts.py
"""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import patch_ctexp_correlated_tier_v0 as patch  # noqa: E402
import setup_ctexp_tenant as setup  # noqa: E402


def _synthetic_body() -> dict:
    return {
        "trading_mode": "paper",
        "max_open_positions": 36,
        "sell_policy": {
            "mode": "active",
            "rotation": {
                "prefer_full_close": False,
                "grid_profit_full_close": False,
                "stagnant_rotation_enabled": False,
                "stagnant_slack_slots": 2,
                "stagnant_gain_pct": 8.0,
                "stagnant_idle_hours": 24.0,
                "stagnant_gain_pct_stable": 8.0,
            },
            "correlated_tier": {
                "enabled": False,
                "tenants": ["default", "henry"],
                "groups": {
                    "us_stock": {
                        "drawdown_pct": 5.0,
                        "window_sec": 600,
                        "trailing_take_profit": {"trail_pct": 3.5, "arm_gain_pct": 10},
                    },
                    "crypto_market": {
                        "proxy_symbols": ["BTC/USDT", "ETH/USDT"],
                        "member_symbols": "*",
                        "drawdown_pct": 4.0,
                    },
                },
                "eval_interval_sec": 5,
                "flag_ttl_sec": 30,
            },
        },
    }


class TestDeepMergePreservesGroups(unittest.TestCase):
    def test_experiment_overlay_keeps_groups_and_siblings(self):
        base = _synthetic_body()
        groups_before = copy.deepcopy(base["sell_policy"]["correlated_tier"]["groups"])
        merged = patch._deep_merge(base, patch.EXPERIMENT)

        ct = merged["sell_policy"]["correlated_tier"]
        rot = merged["sell_policy"]["rotation"]

        self.assertEqual(merged["max_open_positions"], 18)
        self.assertIs(ct["enabled"], True)
        self.assertEqual(ct["groups"], groups_before)
        self.assertEqual(ct["eval_interval_sec"], 5)
        self.assertEqual(ct["flag_ttl_sec"], 30)
        self.assertEqual(ct["tenants"], ["default", "henry"])
        self.assertIs(rot["stagnant_rotation_enabled"], True)
        self.assertEqual(rot["stagnant_slack_slots"], 8)
        self.assertEqual(rot["stagnant_gain_pct"], 6.0)
        self.assertEqual(rot["stagnant_idle_hours"], 12.0)
        # Unrelated rotation keys must survive the nested merge.
        self.assertIs(rot["prefer_full_close"], False)
        self.assertEqual(rot["stagnant_gain_pct_stable"], 8.0)
        self.assertIn("_experiment_ctexp_v0", ct)
        self.assertIn("_experiment_ctexp_v0", merged)

    def test_shallow_replace_would_wipe_groups_deep_merge_does_not(self):
        base = _synthetic_body()
        shallow = copy.deepcopy(base)
        shallow["sell_policy"]["correlated_tier"] = patch.EXPERIMENT["sell_policy"][
            "correlated_tier"
        ]
        self.assertNotIn("groups", shallow["sell_policy"]["correlated_tier"])

        deep = patch._deep_merge(base, patch.EXPERIMENT)
        self.assertIn("us_stock", deep["sell_policy"]["correlated_tier"]["groups"])
        self.assertIn("crypto_market", deep["sell_policy"]["correlated_tier"]["groups"])
        self.assertEqual(
            deep["sell_policy"]["correlated_tier"]["groups"]["us_stock"]["drawdown_pct"],
            5.0,
        )


class TestSetupConfigOverride(unittest.TestCase):
    def test_new_tenant_doc_is_headless_without_owner_chat(self):
        doc = setup._new_tenant_doc({"max_open_positions": 36, "dry_run_defaults": {}})
        tg = doc["telegram"]
        self.assertEqual(tg.get("owner_chat_id"), "")
        self.assertTrue(tg.get("headless"))

    def test_desired_body_only_overrides_trading_mode(self):
        disk = setup._load_disk_config()
        body = setup.desired_tenant_config_body(disk)
        self.assertEqual(body["trading_mode"], "paper")
        expected = copy.deepcopy(disk)
        expected["trading_mode"] = "paper"
        self.assertEqual(body, expected)
        # Paper is the genuine paper-ledger mode, not simulated-live.
        self.assertNotEqual(disk.get("trading_mode"), "paper")
        self.assertEqual(disk.get("trading_mode"), "live")

    def test_disk_config_keeps_correlated_tier_groups_for_later_merge(self):
        disk = setup._load_disk_config()
        paper = setup.desired_tenant_config_body(disk)
        groups = ((paper.get("sell_policy") or {}).get("correlated_tier") or {}).get(
            "groups"
        )
        self.assertIsInstance(groups, dict)
        self.assertIn("us_stock", groups)
        self.assertIn("crypto_market", groups)
        merged = patch._deep_merge(paper, patch.EXPERIMENT)
        self.assertEqual(merged["sell_policy"]["correlated_tier"]["groups"], groups)
        self.assertIs(merged["sell_policy"]["correlated_tier"]["enabled"], True)

    def test_watchlist_disk_fallback_reads_repo_files(self):
        coins = setup._dedupe_coins(
            setup._coins_from_watchlist_file(setup.WATCHLIST_PATH)
            + setup._coins_from_watchlist_file(setup.EXPANSION_PATH)
        )
        self.assertGreater(len(coins), 0)
        symbols = [c["symbol"] for c in coins]
        self.assertEqual(len(symbols), len(set(symbols)))
        raw = json.loads(setup.WATCHLIST_PATH.read_text(encoding="utf-8"))
        self.assertIn(raw["coins"][0]["symbol"], symbols)


if __name__ == "__main__":
    unittest.main()
