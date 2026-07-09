"""P0 sell-policy fixes: overlay for trending coins + trail-exclusive guard."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.actions import SELL_PARTIAL_20, SELL_PARTIAL_30
from core.models import MarketContext
from strategies.registry import resolve_strategy_params
from strategies.sell_rotation_policy import filter_trail_exclusive


def _trail_params(*, arm: float = 15.0, enabled: bool = True) -> dict:
    return {
        "volatility_tier": "stable",
        "trailing_take_profit": {
            "enabled": enabled,
            "mode": "live",
            "arm_gain_pct": arm,
            "trail_pct": 6.0,
            "min_gain_pct": 10.0,
        },
    }


class TestCmcTrendingSellOverlay(unittest.TestCase):
    def test_stable_position_gets_stable_sell_overlay(self):
        cfg = type("Cfg", (), {})()
        cfg._raw = {
            "strategies": [],
            "altcoin_social": {"cmc_sell_min_confidence": 70},
            "stable_altcoin": {
                "enabled": True,
                "take_profit_tiers": [60, 100, 150],
                "trailing_take_profit": {
                    "enabled": True,
                    "mode": "live",
                    "arm_gain_pct": 15,
                },
                "exit_ladder": {"enabled": True, "tiers": [0.3, 0.3, 0.4]},
            },
            "volatile_altcoin": {"enabled": True},
            "dry_run_defaults": {},
        }
        cfg.raw = cfg._raw
        cfg.stable_altcoin_config = cfg._raw["stable_altcoin"]
        cfg.volatile_altcoin_config = cfg._raw["volatile_altcoin"]
        cfg.altcoin_social_config = cfg._raw["altcoin_social"]
        cfg.dry_run_defaults = {}

        with patch("strategies.registry.get_bot_config", return_value=cfg), \
             patch("strategies.registry.is_dry_run_enhanced", return_value=False), \
             patch("strategies.registry._resolve_volatility_tier", return_value="stable"):
            params = resolve_strategy_params(
                {"symbol": "AARK/USDT", "timeframe": "1h", "source": "cmc_trending"},
                has_position=True,
                atr_pct=3.5,
            )

        self.assertTrue(params["trailing_take_profit"]["enabled"])
        self.assertEqual(params["take_profit_tiers"], [60, 100, 150])
        self.assertIn("stable", params.get("strategy_profile", ""))

    def test_no_position_keeps_social_buy_profile_only(self):
        cfg = type("Cfg", (), {})()
        cfg._raw = {
            "strategies": [],
            "altcoin_social": {"cmc_sell_min_confidence": 70},
            "stable_altcoin": {
                "enabled": True,
                "trailing_take_profit": {"enabled": True, "mode": "live", "arm_gain_pct": 15},
            },
            "volatile_altcoin": {"enabled": True},
            "dry_run_defaults": {},
        }
        cfg.raw = cfg._raw
        cfg.stable_altcoin_config = cfg._raw["stable_altcoin"]
        cfg.volatile_altcoin_config = cfg._raw["volatile_altcoin"]
        cfg.altcoin_social_config = cfg._raw["altcoin_social"]
        cfg.dry_run_defaults = {}

        with patch("strategies.registry.get_bot_config", return_value=cfg), \
             patch("strategies.registry.is_dry_run_enhanced", return_value=False), \
             patch("strategies.registry._resolve_volatility_tier", return_value="stable"):
            params = resolve_strategy_params(
                {"symbol": "AARK/USDT", "timeframe": "1h", "source": "cmc_trending"},
                has_position=False,
                atr_pct=3.5,
            )

        self.assertNotIn("take_profit_tiers", params)
        self.assertNotIn("trailing_take_profit", params)


class TestTrailExclusiveGuard(unittest.TestCase):
    def _cfg(self, **overrides):
        base = {
            "trail_exclusive": True,
            "arm_gain_pct": 15.0,
            "trail_exit_full_close": True,
            "pre_arm_ta_allowed": True,
            "pre_arm_min_gain_pct": 10.0,
            "pre_arm_max_gain_pct": 15.0,
        }
        base.update(overrides)
        return base

    def _market(self, entry: float, price: float) -> MarketContext:
        return MarketContext(
            symbol="TST/USDT",
            timeframe="1h",
            current_price=price,
            has_position=True,
            average_entry=entry,
        )

    def test_passes_when_trail_tp_not_configured(self):
        market = self._market(1.0, 1.11)
        pos = {"recent_high": 1.11}
        cands = [(SELL_PARTIAL_20, 3, "technical"), (SELL_PARTIAL_30, 3, "bb_upper")]
        kept, blocked = filter_trail_exclusive(cands, market, pos, self._cfg(), strategy_params={})
        self.assertEqual(len(kept), 2)
        self.assertEqual(blocked, [])

    def test_passes_when_trail_not_armed_below_peak_threshold(self):
        market = self._market(1.0, 1.112)
        pos = {"recent_high": 1.112}
        cands = [(SELL_PARTIAL_20, 3, "technical")]
        kept, blocked = filter_trail_exclusive(
            cands, market, pos, self._cfg(), strategy_params=_trail_params(arm=15.0),
        )
        self.assertEqual(kept, cands)
        self.assertEqual(blocked, [])

    def test_blocks_technical_when_trail_armed(self):
        market = self._market(1.0, 1.20)
        pos = {"recent_high": 1.20}
        cands = [(SELL_PARTIAL_20, 3, "technical"), (SELL_PARTIAL_30, 3, "bb_upper")]
        kept, blocked = filter_trail_exclusive(
            cands, market, pos, self._cfg(), strategy_params=_trail_params(arm=15.0),
        )
        self.assertEqual(kept, [])
        self.assertEqual(set(blocked), {"technical", "bb_upper"})

    def test_pre_arm_allows_technical_only_in_dead_zone_when_armed_peak_false(self):
        """11% gain: TA allowed, structure still open when trail not armed."""
        market = self._market(1.0, 1.11)
        pos = {"recent_high": 1.11}
        cands = [(SELL_PARTIAL_20, 3, "technical"), (SELL_PARTIAL_30, 3, "bb_upper")]
        kept, blocked = filter_trail_exclusive(
            cands, market, pos, self._cfg(), strategy_params=_trail_params(arm=15.0),
        )
        self.assertEqual(kept, cands)
        self.assertEqual(blocked, [])


if __name__ == "__main__":
    unittest.main()