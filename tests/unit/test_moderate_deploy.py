"""Moderate deploy size boost — NEUTRAL/ON only, RISK_OFF unchanged."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from risk.moderate_deploy import (
    effective_max_total_multiplier,
    moderate_deploy_config,
    moderate_deploy_enabled,
    size_boost_for_regime,
)


class TestModerateDeployConfig(unittest.TestCase):
    def test_disabled_by_default(self):
        self.assertFalse(moderate_deploy_enabled({}))
        self.assertFalse(moderate_deploy_enabled({"risk": {}}))

    def test_enabled_from_risk_section(self):
        cfg = {"risk": {"moderate_deploy": {"enabled": True}}}
        self.assertTrue(moderate_deploy_enabled(cfg))
        c = moderate_deploy_config(cfg)
        self.assertAlmostEqual(c["size_boost_neutral"], 1.5)


class TestSizeBoostForRegime(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "risk": {
                "moderate_deploy": {
                    "enabled": True,
                    "size_boost_risk_on": 1.55,
                    "size_boost_neutral": 1.5,
                    "size_boost_risk_off": 1.0,
                    "size_boost_crash": 1.0,
                    "dca_boost_scale": 0.7,
                    "max_boost": 1.75,
                }
            }
        }

    def test_neutral_boost(self):
        self.assertAlmostEqual(
            size_boost_for_regime(self.cfg, "NEUTRAL"), 1.5
        )

    def test_risk_on_boost(self):
        self.assertAlmostEqual(
            size_boost_for_regime(self.cfg, "RISK_ON"), 1.55
        )

    def test_risk_off_no_boost(self):
        self.assertAlmostEqual(
            size_boost_for_regime(self.cfg, "RISK_OFF"), 1.0
        )

    def test_crash_no_boost(self):
        self.assertAlmostEqual(
            size_boost_for_regime(self.cfg, "CRASH"), 1.0
        )

    def test_disabled_always_one(self):
        off = {"risk": {"moderate_deploy": {"enabled": False}}}
        self.assertAlmostEqual(size_boost_for_regime(off, "NEUTRAL"), 1.0)

    def test_dca_milder(self):
        # 1 + (1.5-1)*0.7 = 1.35
        self.assertAlmostEqual(
            size_boost_for_regime(self.cfg, "NEUTRAL", is_dca=True), 1.35
        )

    def test_dca_risk_off_still_one(self):
        self.assertAlmostEqual(
            size_boost_for_regime(self.cfg, "RISK_OFF", is_dca=True), 1.0
        )

    def test_max_total_ceiling_only_when_boosting(self):
        self.assertAlmostEqual(
            effective_max_total_multiplier(self.cfg, base_max=1.25, boost=1.0),
            1.25,
        )
        self.assertAlmostEqual(
            effective_max_total_multiplier(self.cfg, base_max=1.25, boost=1.5),
            1.6,
        )


class TestDynamicSizeWiresBoost(unittest.TestCase):
    def test_neutral_increases_size(self):
        from core.config import BotConfig
        from core.models import TradeOrder
        from data_manager import get_config
        from risk.risk_manager import RiskManager

        raw = dict(get_config())
        raw["trading_mode"] = "paper"
        raw["aggression"] = {"max_position_multiplier": 2.0, "min_trust_for_live": 70}
        raw["risk"] = dict(raw.get("risk") or {})
        raw["risk"]["moderate_deploy"] = {
            "enabled": True,
            "size_boost_neutral": 1.5,
            "size_boost_risk_off": 1.0,
            "max_total_multiplier": 2.0,
            "max_boost": 1.75,
        }
        raw["risk"]["min_size_multiplier"] = 0.25
        cfg = BotConfig()
        cfg._raw = raw
        risk = RiskManager(cfg)
        order = TradeOrder("BUY", "TEST/USDT", 1.0, 0, usdt_amount=1000)

        bias_n = {
            "active": True,
            "apply_size_mult": True,
            "size_mult": 1.0,
            "regime": "NEUTRAL",
            "source": "test",
        }
        bias_off = {
            "active": True,
            "apply_size_mult": True,
            "size_mult": 0.35,
            "regime": "RISK_OFF",
            "source": "test",
        }

        with patch(
            "services.market_policy_fusion.get_global_market_bias",
            return_value=bias_n,
        ), patch(
            "intelligence.memory.cache.get_size_bias", return_value=1.0
        ), patch(
            "intelligence.memory.cache.get_coin_profile", return_value=None
        ):
            sized_n, fac_n = risk._dynamic_size(
                1000.0, order, "4h", "grid", 70.0, 50.0, {"atr_pct": 3.0}
            )

        with patch(
            "services.market_policy_fusion.get_global_market_bias",
            return_value=bias_off,
        ), patch(
            "intelligence.memory.cache.get_size_bias", return_value=1.0
        ), patch(
            "intelligence.memory.cache.get_coin_profile", return_value=None
        ):
            # disable boost to get baseline under RISK_OFF path
            raw["risk"]["moderate_deploy"]["enabled"] = False
            sized_off_base, _ = risk._dynamic_size(
                1000.0, order, "4h", "grid", 70.0, 50.0, {"atr_pct": 3.0}
            )
            raw["risk"]["moderate_deploy"]["enabled"] = True
            sized_off, fac_off = risk._dynamic_size(
                1000.0, order, "4h", "grid", 70.0, 50.0, {"atr_pct": 3.0}
            )

        self.assertGreater(fac_n.get("moderate_deploy_mult", 1), 1.0)
        self.assertAlmostEqual(fac_off.get("moderate_deploy_mult", 1), 1.0)
        # RISK_OFF size unchanged by flag
        self.assertAlmostEqual(sized_off, sized_off_base, places=2)
        # NEUTRAL with boost should be larger than pure RISK_OFF size
        self.assertGreater(sized_n, sized_off)


if __name__ == "__main__":
    unittest.main()
