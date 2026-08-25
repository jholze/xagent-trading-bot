from __future__ import annotations

import unittest

from strategies.short_policy import (
    auto_short_notional_usdt,
    is_auto_short_source,
    resolve_short_params,
    shorts_enabled,
)


CFG = {
    "shorts": {
        "enabled": True,
        "leverage_default": 2,
        "leverage_cap": 5,
        "auto_sources": ["rsi_sell", "oracle_climax_harvest"],
        "volatile": {"time_cap_hours": 4, "stop_margin_pct": 0.12, "market_cap_min_usd": 5e7},
        "stable": {"time_cap_hours": 8, "stop_margin_pct": 0.08, "market_cap_min_usd": 1e8},
        "coins": {"H/USDT": {"leverage": 3, "time_cap_hours": 3}},
    }
}


class TestShortPolicy(unittest.TestCase):
    def test_disabled_without_block(self):
        self.assertFalse(shorts_enabled({}))

    def test_tier_split(self):
        v = resolve_short_params(tier="volatile", config_raw=CFG)
        s = resolve_short_params(tier="stable", config_raw=CFG)
        self.assertEqual(v["time_cap_hours"], 4)
        self.assertEqual(s["time_cap_hours"], 8)
        self.assertGreater(s["market_cap_min_usd"], v["market_cap_min_usd"])

    def test_coin_and_lot_override(self):
        p = resolve_short_params(symbol="H/USDT", tier="volatile", config_raw=CFG)
        self.assertEqual(p["leverage"], 3.0)
        self.assertEqual(p["time_cap_hours"], 3)
        p2 = resolve_short_params(
            symbol="H/USDT",
            tier="volatile",
            lot={"leverage": 4},
            config_raw=CFG,
        )
        self.assertEqual(p2["leverage"], 4.0)

    def test_auto_allowlist_excludes_bb_upper(self):
        self.assertTrue(is_auto_short_source("rsi_sell", CFG))
        self.assertFalse(is_auto_short_source("bb_upper", CFG))
        self.assertFalse(is_auto_short_source("trailing_take_profit", CFG))

    def test_auto_notional_fraction(self):
        self.assertAlmostEqual(
            auto_short_notional_usdt(1000, cap=500, config_raw={"shorts": {"auto_notional_pct": 0.35}}),
            350,
        )
