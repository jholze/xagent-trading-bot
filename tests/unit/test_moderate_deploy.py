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

    def test_cash_rich_extra_mult(self):
        self.cfg["risk"]["moderate_deploy"]["cash_rich_pct"] = 50
        self.cfg["risk"]["moderate_deploy"]["cash_rich_extra_mult"] = 1.3
        self.cfg["risk"]["moderate_deploy"]["max_boost"] = 3.0
        base = size_boost_for_regime(self.cfg, "NEUTRAL", cash_pct=20)
        rich = size_boost_for_regime(self.cfg, "NEUTRAL", cash_pct=80)
        self.assertAlmostEqual(base, 1.5)
        self.assertAlmostEqual(rich, 1.5 * 1.3)

    def test_max_total_ceiling_only_when_boosting(self):
        self.assertAlmostEqual(
            effective_max_total_multiplier(self.cfg, base_max=1.25, boost=1.0),
            1.25,
        )
        self.assertAlmostEqual(
            effective_max_total_multiplier(self.cfg, base_max=1.25, boost=1.5),
            1.6,
        )

    def _cash_rich(self, **overrides):
        md = self.cfg["risk"]["moderate_deploy"]
        md["cash_rich_pct"] = 50
        md["cash_rich_extra_mult"] = 1.3
        md["max_boost"] = 3.0
        md.update(overrides)
        return self.cfg

    def test_risk_off_cash_rich_no_boost(self):
        # Old: 1.0 * cash_rich_extra 1.3 = 1.3 (guard was CRASH-only).
        cfg = self._cash_rich()
        self.assertAlmostEqual(
            size_boost_for_regime(cfg, "RISK_OFF", cash_pct=80), 1.0
        )

    def test_warmup_cash_rich_no_boost(self):
        # Old: 1.0 * 1.3 = 1.3 (WARMUP was not in the cash-rich skip).
        cfg = self._cash_rich()
        self.assertAlmostEqual(
            size_boost_for_regime(cfg, "WARMUP", cash_pct=80), 1.0
        )

    def test_config_size_boost_risk_off_clamped_to_one(self):
        # Old: size_boost_risk_off 1.25 passed through as 1.25.
        cfg = self._cash_rich(
            size_boost_risk_off=1.25,
            size_boost_warmup=1.25,
            size_boost_crash=1.5,
        )
        parsed = moderate_deploy_config(cfg)
        self.assertAlmostEqual(parsed["size_boost_risk_off"], 1.0)
        self.assertAlmostEqual(parsed["size_boost_warmup"], 1.0)
        self.assertAlmostEqual(parsed["size_boost_crash"], 1.0)
        self.assertAlmostEqual(size_boost_for_regime(cfg, "RISK_OFF"), 1.0)
        self.assertAlmostEqual(size_boost_for_regime(cfg, "WARMUP"), 1.0)
        # Live-bug combo: 1.25 * 1.3 = 1.625 under the old rule.
        self.assertAlmostEqual(
            size_boost_for_regime(cfg, "RISK_OFF", cash_pct=80), 1.0
        )

    def test_crash_cash_rich_no_boost(self):
        # Pin: CRASH already skipped cash-rich extra; must stay 1.0.
        cfg = self._cash_rich()
        self.assertAlmostEqual(
            size_boost_for_regime(cfg, "CRASH", cash_pct=80), 1.0
        )


class TestAbsentOracleNoBoost390(unittest.TestCase):
    """#390 / #384 Option 2: regime=None must never size-boost (same as UNKNOWN)."""

    def setUp(self):
        self.md = {
            "enabled": True,
            "size_boost_risk_on": 1.55,
            "size_boost_neutral": 1.5,
            "size_boost_risk_off": 1.0,
            "size_boost_crash": 1.0,
            "size_boost_warmup": 1.0,
            "size_boost_default": 1.35,
            "dca_boost_scale": 0.7,
            "max_boost": 3.0,
            "cash_rich_pct": 50.0,
            "cash_rich_extra_mult": 1.3,
            "apply_to_dca": True,
        }
        self.cfg = {"risk": {"moderate_deploy": dict(self.md)}}

    def test_none_cash_rich_does_not_apply_extra_mult(self):
        # Old: size_boost_default 1.35 * cash_rich_extra 1.3 = 1.755.
        self.assertAlmostEqual(
            size_boost_for_regime(self.cfg, None, cash_pct=80), 1.0
        )

    def test_none_dca_does_not_boost(self):
        # Old: 1 + (1.35-1)*0.7 = 1.245.
        self.assertAlmostEqual(
            size_boost_for_regime(self.cfg, None, is_dca=True), 1.0
        )

    def test_none_equals_unknown_under_log_and_deny(self):
        # size_boost_for_regime is mode-agnostic; still prove both config paths.
        for mode in ("log", "deny"):
            cfg = {
                "risk": {
                    "fail_closed_guards": mode,
                    "moderate_deploy": dict(self.md),
                }
            }
            none_v = size_boost_for_regime(cfg, None, cash_pct=80, is_dca=True)
            unk_v = size_boost_for_regime(cfg, "UNKNOWN", cash_pct=80, is_dca=True)
            self.assertEqual(none_v, unk_v, msg=f"mode={mode}")
            self.assertAlmostEqual(none_v, 1.0, msg=f"mode={mode}")

    def test_named_regimes_unchanged(self):
        # Must not regress #376: RISK_OFF / CRASH / WARMUP stay 1.0.
        self.assertAlmostEqual(size_boost_for_regime(self.cfg, "RISK_ON"), 1.55)
        self.assertAlmostEqual(size_boost_for_regime(self.cfg, "NEUTRAL"), 1.5)
        self.assertAlmostEqual(size_boost_for_regime(self.cfg, "RISK_OFF"), 1.0)
        self.assertAlmostEqual(size_boost_for_regime(self.cfg, "CRASH"), 1.0)
        self.assertAlmostEqual(size_boost_for_regime(self.cfg, "WARMUP"), 1.0)


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
        ), patch.object(
            risk, "_available_usdt", return_value=10_000.0
        ), patch.object(
            risk, "_portfolio_equity", return_value=100_000.0  # cash 10% — not rich
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
        ), patch.object(
            risk, "_available_usdt", return_value=10_000.0
        ), patch.object(
            risk, "_portfolio_equity", return_value=100_000.0
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
        # RISK_OFF size unchanged by flag when not cash-rich
        self.assertAlmostEqual(sized_off, sized_off_base, places=2)
        # NEUTRAL with boost should be larger than pure RISK_OFF size
        self.assertGreater(sized_n, sized_off)

    def _rm_with_md(self, **md_overrides):
        from core.config import BotConfig
        from data_manager import get_config
        from risk.risk_manager import RiskManager

        raw = dict(get_config())
        raw["trading_mode"] = "paper"
        raw["aggression"] = {"max_position_multiplier": 1.25, "min_trust_for_live": 70}
        raw["risk"] = dict(raw.get("risk") or {})
        md = {
            "enabled": True,
            "size_boost_neutral": 1.5,
            "size_boost_risk_off": 1.25,
            "size_boost_warmup": 1.25,
            "size_boost_crash": 1.0,
            "max_total_multiplier": 2.0,
            "max_boost": 1.75,
            "cash_rich_pct": 50,
            "cash_rich_extra_mult": 1.3,
            "apply_to_dca": True,
            "dca_boost_scale": 0.9,
        }
        md.update(md_overrides)
        raw["risk"]["moderate_deploy"] = md
        raw["risk"]["min_size_multiplier"] = 0.25
        cfg = BotConfig()
        cfg._raw = raw
        return RiskManager(cfg), raw

    def test_risk_off_cash_rich_does_not_lift_dynamic_size(self):
        from core.models import TradeOrder
        from risk.moderate_deploy import effective_max_total_multiplier

        risk, _raw = self._rm_with_md()
        order = TradeOrder("BUY", "TEST/USDT", 1.0, 0, usdt_amount=1000)
        bias_off = {
            "active": True,
            "apply_size_mult": True,
            "size_mult": 0.35,
            "regime": "RISK_OFF",
            "source": "test",
        }
        with patch(
            "services.market_policy_fusion.get_global_market_bias",
            return_value=bias_off,
        ), patch(
            "intelligence.memory.cache.get_size_bias", return_value=1.0
        ), patch(
            "intelligence.memory.cache.get_coin_profile", return_value=None
        ), patch.object(
            risk, "_available_usdt", return_value=80_000.0
        ), patch.object(
            risk, "_portfolio_equity", return_value=100_000.0
        ), patch(
            "risk.moderate_deploy.effective_max_total_multiplier",
            wraps=effective_max_total_multiplier,
        ) as spy:
            sized, fac = risk._dynamic_size(
                1000.0, order, "4h", "grid", 70.0, 50.0, {"atr_pct": 3.0}
            )
        # Old: 1.25 * 1.3 = 1.625, and effective_max_total_multiplier lifted max_mult.
        self.assertAlmostEqual(fac.get("moderate_deploy_mult", 1), 1.0)
        spy.assert_not_called()
        self.assertAlmostEqual(fac.get("global_size_mult", 1), 0.35)

        risk_off, _raw_off = self._rm_with_md(enabled=False)
        with patch(
            "services.market_policy_fusion.get_global_market_bias",
            return_value=bias_off,
        ), patch(
            "intelligence.memory.cache.get_size_bias", return_value=1.0
        ), patch(
            "intelligence.memory.cache.get_coin_profile", return_value=None
        ), patch.object(
            risk_off, "_available_usdt", return_value=80_000.0
        ), patch.object(
            risk_off, "_portfolio_equity", return_value=100_000.0
        ):
            sized_base, _ = risk_off._dynamic_size(
                1000.0, order, "4h", "grid", 70.0, 50.0, {"atr_pct": 3.0}
            )
        self.assertAlmostEqual(sized, sized_base, places=2)


class TestDcaPathNoBoostUnderRiskOff(unittest.TestCase):
    """DCA branch in RiskManager (~823-860) must not enlarge RISK_OFF cash-rich adds."""

    def test_dca_risk_off_cash_rich_sized_equals_base(self):
        from core.config import BotConfig
        from core.models import TradeOrder
        from data_manager import get_config
        from risk.risk_manager import RiskManager

        raw = dict(get_config())
        raw["trading_mode"] = "paper"
        raw["max_usdt_per_trade"] = 4500
        raw["max_position_percent"] = 80
        raw["max_open_positions"] = 50
        raw["risk"] = dict(raw.get("risk") or {})
        raw["risk"]["min_trade_usdt"] = 100
        raw["risk"]["moderate_deploy"] = {
            "enabled": True,
            "size_boost_risk_off": 1.25,
            "size_boost_warmup": 1.25,
            "size_boost_crash": 1.0,
            "max_boost": 2.5,
            "max_total_multiplier": 2.6,
            "apply_to_dca": True,
            "dca_boost_scale": 0.9,
            "cash_rich_pct": 50,
            "cash_rich_extra_mult": 1.3,
        }
        cfg = BotConfig()
        cfg._raw = raw
        risk = RiskManager(cfg)
        base_usdt = 1000.0
        order = TradeOrder(
            type="BUY",
            symbol="TAG/USDT",
            price=0.0013,
            amount=0,
            usdt_amount=base_usdt,
            signal="BUY_DCA",
        )
        bias_off = {
            "active": True,
            "apply_size_mult": True,
            "size_mult": 0.35,
            "regime": "RISK_OFF",
            "source": "test",
        }
        with patch(
            "services.market_policy_fusion.get_global_market_bias",
            return_value=bias_off,
        ), patch.object(
            risk, "_portfolio_equity", return_value=100_000.0
        ), patch.object(
            risk, "_available_usdt", return_value=80_000.0
        ), patch.object(
            risk, "_spendable_usdt", return_value=80_000.0
        ), patch.object(
            risk, "_cash_floor_blocked", return_value=None
        ), patch.object(
            risk, "_trade_cooldown_blocked", return_value=(False, "")
        ), patch.object(
            risk, "_daily_buy_limit_blocked", return_value=None
        ), patch.object(
            risk, "_daily_dca_usdt_limit_blocked", return_value=None
        ), patch.object(
            risk, "_daily_buys_count", return_value=0
        ), patch.object(
            risk, "_daily_dca_usdt_sum", return_value=0.0
        ), patch(
            "risk.risk_manager.count_open_full_slots", return_value=1
        ), patch(
            "risk.risk_manager.get_position",
            return_value={
                "amount": 1_000_000,
                "sold_percent": 0,
                "average_entry": 0.0013,
                "strategy_tier": "volatile",
            },
        ), patch(
            "risk.risk_manager.load_trade_history",
            return_value={"virtual_balance": 80_000.0},
        ), patch(
            "strategies.position_lock.dca_blocked", return_value=(False, "")
        ):
            decision = risk.evaluate(order, "1h", source="dca")

        self.assertTrue(decision.approved, decision.message)
        # Old: DCA milder 1.225 * cash-rich 1.3 = 1.5925 → sized 1592.50
        self.assertAlmostEqual(decision.order.usdt_amount, base_usdt, places=2)


if __name__ == "__main__":
    unittest.main()
