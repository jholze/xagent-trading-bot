"""Unit tests for dca_sniper pure + checklist + engine dry paths."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from services.dca_sniper.checklist import analyze_candidate
from services.dca_sniper.pure import (
    CandidateView,
    cash_plan,
    compute_heavy_size,
    dynamic_focus_slots,
    is_grid_excluded,
    rank_priority,
    score_checklist,
    select_focus_batch,
)


class TestDcaSniperPure(unittest.TestCase):
    def test_grid_exclude(self):
        self.assertTrue(is_grid_excluded(strategy_profile="grid"))
        self.assertTrue(is_grid_excluded(strategy_class="grid"))
        self.assertFalse(is_grid_excluded(strategy_profile="volatile_altcoin"))
        self.assertFalse(is_grid_excluded(strategy_profile="grid", exclude_grid=False))

    def test_size_individual_not_fixed(self):
        a = compute_heavy_size(
            rest_notional=2000,
            score=8,
            heavy_min_score=6,
            profile="volatile",
            profile_f={"volatile": 0.85, "default": 0.75},
            spendable_dca=5000,
            max_single_add_usdt=3000,
            max_bag_pct_equity=6,
            equity=100000,
            bag_now=2000,
            min_meaningful_usdt=200,
        )
        b = compute_heavy_size(
            rest_notional=800,
            score=6.5,
            heavy_min_score=6,
            profile="volatile",
            profile_f={"volatile": 0.85, "default": 0.75},
            spendable_dca=5000,
            max_single_add_usdt=3000,
            max_bag_pct_equity=6,
            equity=100000,
            bag_now=800,
            min_meaningful_usdt=200,
        )
        self.assertGreater(a, b)
        self.assertGreater(a, 1000)
        self.assertNotEqual(a, 2000.0)  # not hard-coded 2k necessarily always

    def test_size_zero_when_score_low(self):
        z = compute_heavy_size(
            rest_notional=5000,
            score=3,
            heavy_min_score=6,
            profile="default",
            profile_f={"default": 0.75},
            spendable_dca=10000,
            max_single_add_usdt=3000,
            max_bag_pct_equity=6,
            equity=100000,
            bag_now=5000,
            min_meaningful_usdt=200,
        )
        self.assertEqual(z, 0.0)

    def test_dynamic_n_eff(self):
        cands = [
            CandidateView(symbol="A", usdt_suggest=1000, score=8),
            CandidateView(symbol="B", usdt_suggest=1000, score=7.5),
            CandidateView(symbol="C", usdt_suggest=1000, score=7),
        ]
        n1 = dynamic_focus_slots(
            candidates_yes=cands,
            spendable_dca=1200,
            max_focus_slots=3,
            min_cash_after_focus=100,
        )
        self.assertEqual(n1, 1)
        n3 = dynamic_focus_slots(
            candidates_yes=cands,
            spendable_dca=3500,
            max_focus_slots=3,
            min_cash_after_focus=100,
        )
        self.assertEqual(n3, 3)

    def test_cash_plan_order(self):
        p = cash_plan(
            need_usdt=1000,
            spendable_dca=1000,
            free_cash_above_floor=5000,
            soft_claim_enabled=True,
            soft_claim_max_usdt=500,
        )
        self.assertEqual(p["action"], "DCA_HEAVY")
        p2 = cash_plan(
            need_usdt=1500,
            spendable_dca=1000,
            free_cash_above_floor=2000,
            soft_claim_enabled=True,
            soft_claim_max_usdt=600,
        )
        self.assertEqual(p2["action"], "DCA_HEAVY")
        self.assertGreater(p2["claim"], 0)
        p3 = cash_plan(
            need_usdt=5000,
            spendable_dca=100,
            free_cash_above_floor=200,
            soft_claim_enabled=True,
            soft_claim_max_usdt=50,
        )
        self.assertEqual(p3["action"], "NEED_CASH")

    def test_score_checklist_hard_fail(self):
        layers = {
            "position": {"pass": True, "hard": True, "score": 3},
            "facts": {"pass": False, "hard": True, "score": 0, "reason": "unlock"},
            "ta": {"pass": True, "hard": False, "score": 4},
        }
        score, fails, _ = score_checklist(layers)
        self.assertEqual(score, 0.0)
        self.assertTrue(any("facts" in f for f in fails))

    def test_analyze_candidate_red_bag(self):
        out = analyze_candidate(
            {
                "loss_pct": -20,
                "dca_rounds": 1,
                "max_rounds": 4,
                "notional": 2000,
                "rsi": 28,
                "structure_ok": True,
                "entry_bias": "neutral",
            },
            {"spendable_dca": 3000},
        )
        self.assertTrue(out["heavy_ok"])
        self.assertGreaterEqual(out["score"], 5)

    def test_select_focus_batch(self):
        ranked = [
            CandidateView(
                symbol="A",
                score=8,
                usdt_suggest=800,
                hard_fail=[],
                recovery_hold=False,
            ),
            CandidateView(
                symbol="B",
                score=7.5,
                usdt_suggest=800,
                hard_fail=[],
            ),
            CandidateView(
                symbol="G",
                score=9,
                usdt_suggest=800,
                recovery_hold=True,
            ),
        ]
        batch = select_focus_batch(
            ranked,
            spendable_dca=2000,
            max_focus_slots=3,
            min_cash_after_focus=100,
            open_focus_count=0,
            heavy_min_score=6,
        )
        self.assertEqual(len(batch), 2)
        self.assertEqual(batch[0].symbol, "A")

    def test_rank_priority_prefers_deeper_loss(self):
        a = rank_priority(7, -10, 1000)
        b = rank_priority(7, -30, 1000)
        self.assertGreater(b, a)


class TestDcaSniperEngineDry(unittest.TestCase):
    def test_run_cycle_dry(self):
        from services.dca_sniper.engine import run_cycle

        client = MagicMock()
        client.cash.return_value = {
            "ok": True,
            "spendable_dca": 5000,
            "spendable_new": 4000,
            "cash_floor_abs": 1000,
            "balance": 6000,
            "equity": 100000,
            "cash_mode": "STEADY",
        }
        client.candidates.return_value = {
            "ok": True,
            "candidates": [
                {
                    "symbol": "AAA/USDT",
                    "timeframe": "1h",
                    "average_entry": 1.0,
                    "amount": 2000,
                    "mark": 0.7,
                    "loss_pct": -30,
                    "notional": 1400,
                    "dca_rounds": 0,
                    "max_rounds": 4,
                    "recovery_hold": False,
                    "sniper_focus": False,
                    "strategy_profile": "volatile_altcoin",
                    "strategy_class": "",
                    "has_grid_plan": False,
                    "rsi": 25,
                    "structure_ok": True,
                    "reclaim_ok": True,
                    "free_fall": False,
                    "entry_bias": "prefer",
                }
            ],
        }
        audit = run_cycle(client, dry_run=True)
        self.assertNotIn("error", audit)
        actions = audit.get("actions") or []
        # sharp path: small or heavy depending score/reclaim (both are live DCA intents)
        self.assertTrue(
            any(
                str(a.get("action") or "").startswith("DCA_") or a.get("dry_run")
                for a in actions
            )
        )
        self.assertTrue(any(a.get("dry_run") for a in actions))


class TestDcaSniperConfig(unittest.TestCase):
    def test_disabled_by_default_without_env(self):
        from services.dca_sniper.config import dca_sniper_enabled

        self.assertFalse(dca_sniper_enabled({"dca_sniper": {"enabled": False}}))
        self.assertTrue(dca_sniper_enabled({"dca_sniper": {"enabled": True}}))


if __name__ == "__main__":
    unittest.main()
