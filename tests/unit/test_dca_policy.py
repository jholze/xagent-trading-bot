"""DCA policy v1 pure + wire (#95–#97)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from strategies.dca_policy import (
    DcaContext,
    apply_policy_to_usdt,
    dca_policy_config,
    evaluate_dca_policy,
)


class TestDcaPolicyPure(unittest.TestCase):
    def test_harvest_skip(self):
        r = evaluate_dca_policy(
            DcaContext(cash_mode="HARVEST", fusion_size_mult=0.5),
            dca_policy_config({"policy": {"enabled": True, "harvest_mode": "skip"}}),
        )
        self.assertTrue(r.skip)
        self.assertIn("harvest_skip", r.reason_codes)

    def test_deploy_boosts_mult(self):
        r = evaluate_dca_policy(
            DcaContext(cash_mode="DEPLOY", fusion_size_mult=1.2, score=5, max_score=10),
            dca_policy_config({"policy": {"enabled": True}}),
        )
        self.assertFalse(r.skip)
        self.assertGreater(r.size_mult, 1.0)
        self.assertIn("deploy_boost", r.reason_codes)

    def test_fail_open_fusion_no_skip(self):
        r = evaluate_dca_policy(
            DcaContext(fusion_missing=True, fusion_size_mult=1.0),
            dca_policy_config({"policy": {"enabled": True}}),
        )
        self.assertFalse(r.skip)
        self.assertIn("fail_open_fusion", r.reason_codes)

    def test_skip_beats_size(self):
        r = evaluate_dca_policy(
            DcaContext(block_buys=True, fusion_size_mult=1.5),
            dca_policy_config({"policy": {"enabled": True, "harvest_mode": "skip"}}),
        )
        self.assertTrue(r.skip)

    def test_apply_usdt_shadow_keeps_base(self):
        r = evaluate_dca_policy(
            DcaContext(cash_mode="DEPLOY", fusion_size_mult=1.2),
            dca_policy_config({"policy": {}}),
        )
        self.assertEqual(apply_policy_to_usdt(100.0, r, shadow=True), 100.0)
        live = apply_policy_to_usdt(100.0, r, shadow=False)
        self.assertGreater(live, 100.0)

    def test_cap_spendable_dca(self):
        r = evaluate_dca_policy(
            DcaContext(cash_mode="STEADY", fusion_size_mult=0.85),
            dca_policy_config({"policy": {}}),
        )
        usdt = apply_policy_to_usdt(500.0, r, spendable_dca=120.0, shadow=False)
        self.assertLessEqual(usdt, 120.0)


class TestDcaPolicyWire(unittest.TestCase):
    def test_evaluate_addon_shadow_keeps_candidate_on_harvest(self):
        from core.models import MarketContext
        from strategies.dca import evaluate_dca_addon

        market = MarketContext(
            symbol="ADA/USDT",
            timeframe="4h",
            current_price=0.4,
            has_position=True,
            average_entry=0.5,
        )
        # ~20% loss → inside default loss band
        position = {
            "symbol": "ADA/USDT",
            "amount": 1000,
            "average_entry": 0.5,
            "sold_percent": 0,
            "dca_rounds": 0,
            "dca_recovery_rounds": 0,
        }
        params = {
            "dca": {
                "enabled": True,
                "mode": "live",
                "loss_pct_min": -40,
                "loss_pct_max": -3,
                "interval_hours": 0,
                "max_rounds": 4,
                "fixed_usdt": 200,
                "scoring": {"enabled": False},
                "policy": {
                    "enabled": True,
                    "shadow": True,
                    "harvest_mode": "skip",
                },
            }
        }
        with patch("strategies.dca_context.build_dca_context") as bctx:
            from strategies.dca_policy import DcaContext

            bctx.return_value = DcaContext(
                symbol="ADA/USDT",
                cash_mode="HARVEST",
                fusion_size_mult=0.4,
                score=0,
                max_score=10,
                loss_pct=-20.0,
            )
            cand = evaluate_dca_addon(market, position, params)
        self.assertIsNotNone(cand)
        self.assertIn("policy", cand.rationale)
        self.assertIn("shadow", cand.rationale)

    def test_evaluate_addon_live_skip_returns_none(self):
        from core.models import MarketContext
        from strategies.dca import evaluate_dca_addon

        market = MarketContext(
            symbol="ADA/USDT",
            timeframe="4h",
            current_price=0.4,
            has_position=True,
            average_entry=0.5,
        )
        position = {
            "symbol": "ADA/USDT",
            "amount": 1000,
            "average_entry": 0.5,
            "sold_percent": 0,
            "dca_rounds": 0,
            "dca_recovery_rounds": 0,
        }
        params = {
            "dca": {
                "enabled": True,
                "mode": "live",
                "loss_pct_min": -40,
                "loss_pct_max": -3,
                "interval_hours": 0,
                "max_rounds": 4,
                "fixed_usdt": 200,
                "scoring": {"enabled": False},
                "policy": {
                    "enabled": True,
                    "shadow": False,
                    "harvest_mode": "skip",
                },
            }
        }
        with patch("strategies.dca_context.build_dca_context") as bctx:
            from strategies.dca_policy import DcaContext

            bctx.return_value = DcaContext(
                symbol="ADA/USDT",
                cash_mode="HARVEST",
                fusion_size_mult=0.3,
                loss_pct=-20.0,
            )
            cand = evaluate_dca_addon(market, position, params)
        self.assertIsNone(cand)


if __name__ == "__main__":
    unittest.main()
