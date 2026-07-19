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

    def test_multi_symbol_equal_share_not_full_total(self):
        """DE-style plan: open universe must share total_usdt (not 100% per coin)."""
        plan = plan_scheduled_allocations(
            ["A/USDT", "B/USDT", "C/USDT", "D/USDT"],
            config={
                "enabled": True,
                "total_usdt": 400,
                "min_usdt_per_symbol": 50,
                "max_symbols": 10,
                "interval_days": 7,
            },
            now=datetime(2026, 7, 19, tzinfo=timezone.utc),
            last_run=None,
        )
        self.assertTrue(plan.due)
        self.assertEqual(len(plan.allocations), 4)
        for u in plan.allocations.values():
            self.assertAlmostEqual(u, 100.0, places=2)
        # Single-symbol plan would wrongly assign full 400 — guard against that pattern
        solo = plan_scheduled_allocations(
            ["A/USDT"],
            config={
                "enabled": True,
                "total_usdt": 400,
                "min_usdt_per_symbol": 50,
                "max_symbols": 10,
                "interval_days": 7,
            },
            now=datetime(2026, 7, 19, tzinfo=timezone.utc),
            last_run=None,
        )
        self.assertAlmostEqual(solo.allocations["A/USDT"], 400.0, places=2)
        self.assertNotAlmostEqual(
            plan.allocations["A/USDT"], solo.allocations["A/USDT"], places=2
        )

    def test_stamp_blocks_second_cycle_within_interval(self):
        from strategies.dca_scheduled import stamp_last_scheduled_dca
        from strategies.positions import get_position, positions, update_position

        symbol = "SCHED/USDT"
        tf = "4h"
        backup = {k: dict(v) for k, v in positions.items()}
        positions.clear()
        try:
            update_position(symbol, tf, "BUY", 1.0, 100)
            now = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
            stamp_last_scheduled_dca(symbol, tf, now=now)
            pos = get_position(symbol, tf)
            self.assertIsNotNone(pos.get("last_scheduled_dca_at"))
            self.assertFalse(
                is_schedule_due(
                    now + timedelta(days=2),
                    pos.get("last_scheduled_dca_at"),
                    interval_days=7,
                )
            )
            self.assertTrue(
                is_schedule_due(
                    now + timedelta(days=7, hours=1),
                    pos.get("last_scheduled_dca_at"),
                    interval_days=7,
                )
            )
        finally:
            positions.clear()
            positions.update(backup)

    def test_decision_engine_scheduled_shadow_holds(self):
        """mode=shadow must never leave BUY_DCA as execution action (#102)."""
        from core.models import MarketContext
        from strategies.decision_engine import DecisionEngine
        from strategies.positions import positions, update_position

        symbol = "SHAD/USDT"
        tf = "4h"
        backup = {k: dict(v) for k, v in positions.items()}
        positions.clear()
        try:
            update_position(symbol, tf, "BUY", 1.0, 500)
            market = MarketContext(
                symbol=symbol,
                timeframe=tf,
                current_price=0.9,
                rsi=40,
                lower_bb=0.85,
                atr_pct=3.0,
                has_position=True,
                average_entry=1.0,
                open_positions=1,
                strategy_params={
                    "strategy_profile": "volatile_altcoin",
                    "dca": {
                        "enabled": True,
                        "mode": "live",
                        "scoring": {"enabled": False},
                        "scheduled": {
                            "enabled": True,
                            "mode": "shadow",
                            "total_usdt": 200,
                            "min_usdt_per_symbol": 50,
                            "max_symbols": 10,
                            "apply_policy": False,
                            "respect_spendable_dca": False,
                            "interval_days": 7,
                        },
                    },
                },
            )
            engine = DecisionEngine()
            engine.config.raw.setdefault("regime_detector", {})["enabled"] = False
            engine.config.raw.setdefault("strategy_allocator", {})["enabled"] = False
            engine.config.raw.setdefault("volatile_altcoin", {})["mode"] = "live"
            with patch.object(
                engine,
                "_merge_sell",
                return_value=("HOLD", ["technical"], 50.0, [], "", {}),
            ), patch(
                "strategies.dca_portfolio.should_defer_per_coin_dca",
                return_value=False,
            ), patch(
                "strategies.decision_engine.evaluate_dca_addon",
                return_value=None,
            ):
                analysis = engine.evaluate_with_market(
                    {"symbol": symbol, "timeframe": tf},
                    market,
                )
            self.assertIsNotNone(analysis)
            self.assertEqual(analysis.action, "HOLD")
            self.assertIn("dca_scheduled_shadow", analysis.sources or [])
            self.assertEqual(analysis.shadow_action, "BUY_DCA")
            # Cadence stamped so second evaluate is not due
            from strategies.positions import get_position

            self.assertIsNotNone(get_position(symbol, tf).get("last_scheduled_dca_at"))
        finally:
            positions.clear()
            positions.update(backup)

    def test_decision_engine_sequential_multi_symbol_full_budget(self):
        """4 open coins / total 400: sequential DE fires all with ~$100 each (real stamps).

        First stamp must not block remaining symbols in the same cycle.
        """
        from core.models import MarketContext
        from strategies.decision_engine import DecisionEngine
        from strategies.positions import get_position, positions, update_position

        symbols = ["S1/USDT", "S2/USDT", "S3/USDT", "S4/USDT"]
        tf = "4h"
        backup = {k: dict(v) for k, v in positions.items()}
        positions.clear()
        try:
            for s in symbols:
                update_position(s, tf, "BUY", 1.0, 200)

            sched = {
                "enabled": True,
                "mode": "live",
                "total_usdt": 400,
                "min_usdt_per_symbol": 50,
                "max_symbols": 10,
                "apply_policy": False,
                "respect_spendable_dca": False,
                "interval_days": 7,
            }
            params = {
                "strategy_profile": "volatile_altcoin",
                "dca": {
                    "enabled": True,
                    "mode": "live",
                    "scoring": {"enabled": False},
                    "scheduled": sched,
                },
            }
            engine = DecisionEngine()
            engine.config.raw.setdefault("regime_detector", {})["enabled"] = False
            engine.config.raw.setdefault("strategy_allocator", {})["enabled"] = False
            engine.config.raw.setdefault("volatile_altcoin", {})["mode"] = "live"

            fired = []
            for s in symbols:
                market = MarketContext(
                    symbol=s,
                    timeframe=tf,
                    current_price=0.9,
                    rsi=40,
                    lower_bb=0.85,
                    atr_pct=3.0,
                    has_position=True,
                    average_entry=1.0,
                    open_positions=4,
                    strategy_params=params,
                )
                with patch.object(
                    engine,
                    "_merge_sell",
                    return_value=("HOLD", ["technical"], 50.0, [], "", {}),
                ), patch(
                    "strategies.dca_portfolio.should_defer_per_coin_dca",
                    return_value=False,
                ), patch(
                    "strategies.decision_engine.evaluate_dca_addon",
                    return_value=None,
                ):
                    analysis = engine.evaluate_with_market(
                        {"symbol": s, "timeframe": tf},
                        market,
                    )
                self.assertEqual(
                    analysis.action,
                    "BUY_DCA",
                    msg=f"{s} blocked after prior stamps: sources={analysis.sources}",
                )
                self.assertIn("dca_scheduled", analysis.sources or [])
                self.assertAlmostEqual(float(analysis.dca_usdt), 100.0, places=2)
                fired.append(float(analysis.dca_usdt))
                self.assertIsNotNone(get_position(s, tf).get("last_scheduled_dca_at"))

            self.assertAlmostEqual(sum(fired), 400.0, places=2)

            # Second cycle within interval: no re-fire
            s0 = symbols[0]
            market = MarketContext(
                symbol=s0,
                timeframe=tf,
                current_price=0.9,
                rsi=40,
                lower_bb=0.85,
                atr_pct=3.0,
                has_position=True,
                average_entry=1.0,
                open_positions=4,
                strategy_params=params,
            )
            with patch.object(
                engine,
                "_merge_sell",
                return_value=("HOLD", ["technical"], 50.0, [], "", {}),
            ), patch(
                "strategies.dca_portfolio.should_defer_per_coin_dca",
                return_value=False,
            ), patch(
                "strategies.decision_engine.evaluate_dca_addon",
                return_value=None,
            ):
                analysis2 = engine.evaluate_with_market(
                    {"symbol": s0, "timeframe": tf},
                    market,
                )
            self.assertNotEqual(analysis2.action, "BUY_DCA")
            self.assertNotIn("dca_scheduled", analysis2.sources or [])
        finally:
            positions.clear()
            positions.update(backup)

    def test_portfolio_plan_includes_all_scheduled_shares(self):
        """Scheduled cycle: plan.buys has every due equal-share target, no stamp in collect."""
        from strategies.dca_portfolio import build_portfolio_dca_plan, collect_dca_targets
        from strategies.positions import get_position, positions, update_position

        symbols = ["P1/USDT", "P2/USDT", "P3/USDT", "P4/USDT"]
        tf = "4h"
        backup = {k: dict(v) for k, v in positions.items()}
        positions.clear()
        try:
            for s in symbols:
                update_position(s, tf, "BUY", 1.0, 300)
            coins = [{"symbol": s, "timeframe": tf} for s in symbols]
            prices = {s: 1.0 for s in symbols}
            params = {
                "dca": {
                    "enabled": True,
                    "mode": "live",
                    "scoring": {"enabled": False},
                    "portfolio": {"enabled": True, "min_dca_score": 6},
                    "scheduled": {
                        "enabled": True,
                        "mode": "shadow",
                        "total_usdt": 400,
                        "min_usdt_per_symbol": 50,
                        "max_symbols": 10,
                        "apply_policy": False,
                        "respect_spendable_dca": False,
                        "interval_days": 7,
                    },
                }
            }
            cfg_root = {
                "volatile_altcoin": {
                    "dca": {
                        "portfolio": {"enabled": True, "mode": "shadow"},
                        "scheduled": params["dca"]["scheduled"],
                    }
                }
            }

            def _resolve(coin):
                return {
                    "symbol": coin["symbol"],
                    "timeframe": tf,
                    "strategy_params": params,
                }

            with patch(
                "strategies.dca_portfolio.resolve_coin_config", side_effect=_resolve
            ), patch(
                "strategies.dca_portfolio.resolve_strategy_params", return_value=params
            ), patch(
                "strategies.dca_portfolio.evaluate_dca_addon", return_value=None
            ), patch(
                "strategies.dca.evaluate_dca_addon", return_value=None
            ), patch(
                "strategies.dca_portfolio._build_market",
                side_effect=lambda sym, t, px, pos, sp: MagicMock(
                    symbol=sym, current_price=px
                ),
            ), patch(
                "strategies.dca_portfolio.get_bot_config"
            ) as gbc:
                gbc.return_value.raw = cfg_root
                targets = collect_dca_targets(coins, prices, config_raw=cfg_root)
                # Collect must not stamp — all still due
                for s in symbols:
                    self.assertIsNone(get_position(s, tf).get("last_scheduled_dca_at"))
                self.assertEqual(len(targets), 4)
                self.assertTrue(all(t.source == "dca_scheduled" for t in targets))
                self.assertAlmostEqual(
                    sum(t.usdt_needed for t in targets), 400.0, places=2
                )

                plan = build_portfolio_dca_plan(
                    coins, prices, cash_available=10_000, config_raw=cfg_root
                )
            self.assertEqual(plan.audit.get("mode"), "scheduled")
            self.assertEqual(len(plan.buys), 4)
            self.assertAlmostEqual(float(plan.audit.get("usdt") or 0), 400.0, places=2)
            # Still unstamped until orchestrator shadow/execute
            for s in symbols:
                self.assertIsNone(get_position(s, tf).get("last_scheduled_dca_at"))
        finally:
            positions.clear()
            positions.update(backup)


if __name__ == "__main__":
    unittest.main()
