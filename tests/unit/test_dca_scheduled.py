"""#102 D7 optional scheduled (calendar/weekly-split) DCA."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from strategies.dca_scheduled import (
    evaluate_scheduled_dca_addon,
    is_schedule_due,
    plan_scheduled_allocations,
    scheduled_config,
    scheduled_enabled,
    split_usdt_budget,
)


class TestScheduleDue(unittest.TestCase):
    def test_no_last_run_interval_only_due(self):
        now = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
        self.assertTrue(is_schedule_due(now, None, interval_days=7, weekday=None))

    def test_interval_not_elapsed(self):
        now = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
        last = now - timedelta(days=3)
        self.assertFalse(is_schedule_due(now, last, interval_days=7))

    def test_interval_elapsed(self):
        now = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
        last = now - timedelta(days=7, hours=1)
        self.assertTrue(is_schedule_due(now, last, interval_days=7))

    def test_weekday_gate(self):
        # 2026-07-19 is Sunday (weekday 6)
        sunday = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
        self.assertTrue(is_schedule_due(sunday, None, interval_days=7, weekday=6))
        self.assertFalse(is_schedule_due(sunday, None, interval_days=7, weekday=0))


class TestBudgetSplit(unittest.TestCase):
    def test_equal_split_sums_to_total(self):
        alloc = split_usdt_budget(100.0, ["A/USDT", "B/USDT", "C/USDT", "D/USDT"])
        self.assertEqual(len(alloc), 4)
        self.assertAlmostEqual(sum(alloc.values()), 100.0, places=2)

    def test_min_usdt_reduces_count(self):
        alloc = split_usdt_budget(
            100.0,
            ["A", "B", "C", "D", "E"],
            min_usdt_per_symbol=40.0,
        )
        # 100/3 ≈ 33 < 40, 100/2 = 50 >= 40 → 2 symbols
        self.assertEqual(len(alloc), 2)
        self.assertTrue(all(v + 1e-9 >= 40 for v in alloc.values()))
        self.assertAlmostEqual(sum(alloc.values()), 100.0, places=2)

    def test_disabled_plan(self):
        plan = plan_scheduled_allocations(
            ["A/USDT", "B/USDT"],
            config={"enabled": False, "total_usdt": 500},
        )
        self.assertFalse(plan.due)
        self.assertEqual(plan.allocations, {})
        self.assertEqual(plan.reason, "disabled")

    def test_enabled_due_plan(self):
        plan = plan_scheduled_allocations(
            ["ADA/USDT", "ZBT/USDT"],
            config={
                "enabled": True,
                "total_usdt": 200,
                "min_usdt_per_symbol": 50,
                "max_symbols": 10,
                "interval_days": 7,
            },
            now=datetime(2026, 7, 19, tzinfo=timezone.utc),
            last_run=None,
        )
        self.assertTrue(plan.due)
        self.assertEqual(len(plan.allocations), 2)
        self.assertAlmostEqual(sum(plan.allocations.values()), 200.0, places=2)


class TestScheduledCandidate(unittest.TestCase):
    def test_disabled_returns_none(self):
        market = MagicMock(symbol="ADA/USDT", current_price=1.0)
        pos = {"symbol": "ADA/USDT", "amount": 10, "average_entry": 1.0}
        params = {"dca": {"enabled": True, "scheduled": {"enabled": False}}}
        self.assertIsNone(
            evaluate_scheduled_dca_addon(
                market, pos, params, allocated_usdt=100, config_raw={}
            )
        )

    def test_enabled_builds_candidate(self):
        market = MagicMock(symbol="ADA/USDT", current_price=1.0)
        pos = {"symbol": "ADA/USDT", "amount": 10, "average_entry": 1.0}
        params = {
            "dca": {
                "enabled": True,
                "scheduled": {
                    "enabled": True,
                    "mode": "shadow",
                    "apply_policy": False,
                    "respect_spendable_dca": False,
                },
            }
        }
        cand = evaluate_scheduled_dca_addon(
            market, pos, params, allocated_usdt=120.0, config_raw={}
        )
        self.assertIsNotNone(cand)
        self.assertEqual(cand.source, "dca_scheduled")
        self.assertEqual(cand.usdt_amount, 120.0)
        self.assertTrue(cand.shadow_only)
        self.assertIn("Scheduled DCA", cand.rationale)

    def test_policy_live_skip_blocks(self):
        market = MagicMock(symbol="ADA/USDT", current_price=1.0)
        pos = {"symbol": "ADA/USDT", "amount": 10, "average_entry": 1.2}
        params = {
            "dca": {
                "enabled": True,
                "scheduled": {
                    "enabled": True,
                    "mode": "live",
                    "apply_policy": True,
                    "respect_spendable_dca": False,
                },
                "policy": {
                    "enabled": True,
                    "shadow": False,
                    "harvest_mode": "skip",
                },
            }
        }
        with patch(
            "strategies.dca_context.build_dca_context"
        ) as bctx, patch("strategies.dca_policy.evaluate_dca_policy") as epol:
            from strategies.dca_policy import DcaContext, DcaPolicyResult

            bctx.return_value = DcaContext(
                symbol="ADA/USDT", cash_mode="HARVEST", fusion_size_mult=0.3
            )
            epol.return_value = DcaPolicyResult(
                size_mult=1.0, skip=True, reason_codes=("harvest_skip",)
            )
            cand = evaluate_scheduled_dca_addon(
                market, pos, params, allocated_usdt=100, config_raw={}
            )
        self.assertIsNone(cand)

    def test_scheduled_config_default_off(self):
        cfg = scheduled_config({}, config_raw={})
        self.assertFalse(cfg.get("enabled"))
        self.assertFalse(scheduled_enabled({}, config_raw={}))


class TestCollectIntegration(unittest.TestCase):
    def test_collect_includes_scheduled_when_dip_none(self):
        from strategies.dca_portfolio import collect_dca_targets

        coins = [{"symbol": "ADA/USDT", "timeframe": "4h"}]
        prices = {"ADA/USDT": 0.4}
        pos = {
            "symbol": "ADA/USDT",
            "amount": 100,
            "average_entry": 0.5,
            "sold_percent": 0,
        }
        params = {
            "dca": {
                "enabled": True,
                "mode": "live",
                "scoring": {"enabled": False},
                "portfolio": {"enabled": True, "min_dca_score": 6},
                "scheduled": {
                    "enabled": True,
                    "mode": "shadow",
                    "total_usdt": 200,
                    "min_usdt_per_symbol": 50,
                    "max_symbols": 5,
                    "apply_policy": False,
                    "respect_spendable_dca": False,
                    "interval_days": 7,
                },
            }
        }
        with patch("strategies.dca_portfolio.get_position", return_value=pos), patch(
            "strategies.dca_portfolio.resolve_coin_config",
            return_value={"symbol": "ADA/USDT", "timeframe": "4h", "strategy_params": params},
        ), patch(
            "strategies.dca_portfolio.resolve_strategy_params", return_value=params
        ), patch(
            "strategies.dca_portfolio.evaluate_dca_addon", return_value=None
        ), patch(
            "strategies.dca.evaluate_dca_addon", return_value=None
        ), patch(
            "strategies.dca_portfolio._build_market",
            return_value=MagicMock(symbol="ADA/USDT", current_price=0.4),
        ), patch(
            "strategies.dca_scheduled.collect_open_position_symbols",
            return_value=["ADA/USDT"],
        ), patch(
            "strategies.dca_portfolio.get_bot_config"
        ) as gbc:
            gbc.return_value.raw = {
                "volatile_altcoin": {
                    "dca": {
                        "portfolio": {"enabled": True},
                        "scheduled": params["dca"]["scheduled"],
                    }
                }
            }
            targets = collect_dca_targets(coins, prices, config_raw=gbc.return_value.raw)
        self.assertTrue(targets)
        self.assertEqual(targets[0].source, "dca_scheduled")
        self.assertGreater(targets[0].usdt_needed, 0)


if __name__ == "__main__":
    unittest.main()
