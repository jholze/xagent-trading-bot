"""Adaptive cash policy (Phase 0–1) — pure functions + Risk spendable split."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from risk.cash_policy import (
    MODE_DEPLOY,
    MODE_HARVEST,
    MODE_STEADY,
    compute_dual_spendable,
    evaluate_cash_policy,
    floor_pct_effective,
    is_cash_policy_enabled,
    resolve_cash_mode,
)
from risk.risk_manager import RiskManager


class TestCashPolicyPure(unittest.TestCase):
    def test_mode_deploy_vs_harvest_from_size_mult(self):
        self.assertEqual(resolve_cash_mode(size_mult=1.2), MODE_DEPLOY)
        self.assertEqual(resolve_cash_mode(size_mult=0.85), MODE_STEADY)
        self.assertEqual(resolve_cash_mode(size_mult=0.5), MODE_HARVEST)
        self.assertEqual(
            resolve_cash_mode(size_mult=1.5, block_buys=True), MODE_HARVEST
        )

    def test_floor_pct_risk_on_lower_than_risk_off(self):
        deploy_pct, deploy_mode, _ = floor_pct_effective(
            floor_pct_base=12,
            size_mult=1.1,
            floor_pct_min=5,
            floor_pct_max=25,
        )
        harvest_pct, harvest_mode, _ = floor_pct_effective(
            floor_pct_base=12,
            size_mult=0.4,
            floor_pct_min=5,
            floor_pct_max=25,
        )
        self.assertEqual(deploy_mode, MODE_DEPLOY)
        self.assertEqual(harvest_mode, MODE_HARVEST)
        self.assertLess(deploy_pct, harvest_pct)
        self.assertGreaterEqual(deploy_pct, 5.0)
        self.assertLessEqual(harvest_pct, 25.0)

    def test_floor_pct_clamped_to_max(self):
        pct, mode, reasons = floor_pct_effective(
            floor_pct_base=20,
            size_mult=0.1,
            drawdown_active=True,
            floor_pct_min=5,
            floor_pct_max=25,
            regime_harvest_delta=6,
            drawdown_delta=4,
        )
        # 20+6+4=30 → clamp 25
        self.assertEqual(pct, 25.0)
        self.assertEqual(mode, MODE_HARVEST)
        self.assertIn("clamped", reasons)

    def test_dual_spendable_near_floor_dca_positive_new_zero(self):
        # cash ≈ floor: new entry 0, DCA buffer still available (haircut 0)
        sn, sd, target = compute_dual_spendable(
            cash_total=18_000,
            floor_abs=18_000,
            equity=100_000,
            dca_buffer_usdt=800,
            dca_buffer_pct_equity=0,
            dca_floor_haircut=0.0,
            mode=MODE_STEADY,
        )
        self.assertEqual(sn, 0.0)
        self.assertGreater(sd, 0.0)
        self.assertEqual(sd, 800.0)
        self.assertEqual(target, 800.0)

    def test_evaluate_disabled_legacy_same_spendable(self):
        risk = {"cash_floor_pct": 18, "cash_policy": {"enabled": False}}
        r = evaluate_cash_policy(
            cash_total=20_000,
            basis_for_floor=100_000,
            equity=100_000,
            size_mult=0.3,
            risk_config=risk,
        )
        self.assertFalse(r.enabled)
        self.assertEqual(r.floor_pct_eff, 18.0)
        self.assertEqual(r.floor_abs, 18_000.0)
        self.assertEqual(r.spendable_new, 2_000.0)
        self.assertEqual(r.spendable_dca, 2_000.0)
        self.assertIn("legacy_static_floor", r.reason_codes)

    def test_evaluate_enabled_regime_and_dual(self):
        risk = {
            "cash_floor_pct": 18,
            "cash_policy": {
                "enabled": True,
                "floor_pct_base": 12,
                "floor_pct_min": 5,
                "floor_pct_max": 25,
                "dca_buffer_usdt": 800,
                "dca_buffer_pct_equity": 0,
                "dca_floor_haircut": 0,
                "link_fusion_size_mult": True,
            },
        }
        deploy = evaluate_cash_policy(
            cash_total=18_000,
            basis_for_floor=100_000,
            equity=100_000,
            size_mult=1.2,
            risk_config=risk,
        )
        harvest = evaluate_cash_policy(
            cash_total=18_000,
            basis_for_floor=100_000,
            equity=100_000,
            size_mult=0.4,
            risk_config=risk,
        )
        self.assertTrue(deploy.enabled)
        self.assertTrue(harvest.enabled)
        self.assertEqual(deploy.mode, MODE_DEPLOY)
        self.assertEqual(harvest.mode, MODE_HARVEST)
        self.assertLess(deploy.floor_pct_eff, harvest.floor_pct_eff)
        # Near floor relative to harvest floor (higher): DCA still has buffer when haircut 0
        self.assertGreater(harvest.spendable_dca, 0.0)
        # deploy floor lower → more room for new when cash fixed
        self.assertGreaterEqual(deploy.spendable_new, harvest.spendable_new)

    def test_is_enabled_flag(self):
        self.assertFalse(is_cash_policy_enabled({}))
        self.assertFalse(is_cash_policy_enabled({"cash_policy": {"enabled": False}}))
        self.assertTrue(is_cash_policy_enabled({"cash_policy": {"enabled": True}}))


class TestRiskManagerCashPolicy(unittest.TestCase):
    def _risk_cfg(self, enabled: bool) -> dict:
        return {
            "cash_floor_pct": 18,
            "cash_floor_basis": "initial",
            "min_trade_usdt": 100,
            "drawdown_throttle_pct": 50,
            "cash_policy": {
                "enabled": enabled,
                "floor_pct_base": 18,
                "floor_pct_min": 5,
                "floor_pct_max": 25,
                "floor_basis": "initial",
                "dca_buffer_usdt": 500,
                "dca_buffer_pct_equity": 0,
                "dca_floor_haircut": 0,
                "link_fusion_size_mult": True,
            },
        }

    def test_legacy_dca_and_new_both_blocked_near_floor(self):
        cfg = MagicMock()
        cfg.risk_config = self._risk_cfg(enabled=False)
        cfg.raw = {}
        rm = RiskManager(cfg)
        with patch.object(rm, "_available_usdt", return_value=18_000.0), patch.object(
            rm, "_initial_capital", return_value=100_000.0
        ), patch.object(rm, "_portfolio_equity", return_value=100_000.0), patch.object(
            rm, "_equity_drawdown_pct", return_value=0.0
        ):
            # floor 18k, cash 18k → free 0
            self.assertIsNotNone(rm._cash_floor_blocked(is_dca=False))
            self.assertIsNotNone(rm._cash_floor_blocked(is_dca=True))
            self.assertEqual(rm._spendable_usdt(100_000, is_dca=False), 0.0)
            self.assertEqual(rm._spendable_usdt(100_000, is_dca=True), 0.0)

    def test_adaptive_dca_allowed_new_blocked_near_floor(self):
        cfg = MagicMock()
        cfg.risk_config = self._risk_cfg(enabled=True)
        cfg.raw = {}
        rm = RiskManager(cfg)
        with patch.object(rm, "_available_usdt", return_value=18_000.0), patch.object(
            rm, "_initial_capital", return_value=100_000.0
        ), patch.object(rm, "_portfolio_equity", return_value=100_000.0), patch.object(
            rm, "_equity_drawdown_pct", return_value=0.0
        ), patch.object(
            rm,
            "_market_bias_for_cash",
            return_value={"size_mult": 0.85, "block_buys": False},
        ):
            new_block = rm._cash_floor_blocked(is_dca=False)
            dca_block = rm._cash_floor_blocked(is_dca=True)
            self.assertIsNotNone(new_block)
            self.assertEqual(new_block.code, "cash_floor")
            self.assertIsNone(dca_block)
            self.assertEqual(rm._spendable_usdt(100_000, is_dca=False), 0.0)
            self.assertGreaterEqual(rm._spendable_usdt(100_000, is_dca=True), 100.0)

    def test_adaptive_floor_abs_higher_when_harvest(self):
        cfg = MagicMock()
        cfg.risk_config = self._risk_cfg(enabled=True)
        cfg.raw = {}
        rm = RiskManager(cfg)
        with patch.object(rm, "_available_usdt", return_value=50_000.0), patch.object(
            rm, "_initial_capital", return_value=100_000.0
        ), patch.object(rm, "_portfolio_equity", return_value=100_000.0), patch.object(
            rm, "_equity_drawdown_pct", return_value=0.0
        ):
            with patch.object(
                rm,
                "_market_bias_for_cash",
                return_value={"size_mult": 1.2, "block_buys": False},
            ):
                floor_deploy = rm._cash_floor_abs()
            with patch.object(
                rm,
                "_market_bias_for_cash",
                return_value={"size_mult": 0.3, "block_buys": False},
            ):
                floor_harvest = rm._cash_floor_abs()
        self.assertLess(floor_deploy, floor_harvest)


if __name__ == "__main__":
    unittest.main()
