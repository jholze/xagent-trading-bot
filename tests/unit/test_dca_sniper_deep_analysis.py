"""Prove sniper deep analysis uses Memory/context + policy before size."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from services.dca_sniper.deep_analysis import (
    deep_analyze_candidate,
    enrich_candidate_from_context,
)
from services.dca_sniper.engine import _as_candidate_views, _size_for_row
from strategies.dca_policy import DcaContext, DcaPolicyResult


def _row(**kw):
    base = {
        "symbol": "BLESS/USDT",
        "timeframe": "1h",
        "average_entry": 0.027,
        "amount": 100000,
        "mark": 0.013,
        "loss_pct": -52.0,
        "notional": 1300,
        "dca_rounds": 2,
        "max_rounds": 4,
        "recovery_hold": False,
        "sniper_focus": False,
        "strategy_profile": "volatile_altcoin",
        "reclaim_ok": None,
        "free_fall": None,
        "rsi": 35,
        "atr_pct": 8,
        "funding_rate_pct": 0.001,
        "spendable_dca": 2000,
    }
    base.update(kw)
    return base


def _cfg(**kw):
    c = {
        "deep_analysis_enabled": True,
        "deep_include_rag": True,
        "deep_apply_policy": True,
        "deep_policy_shadow": False,
        "min_dd_pct_for_dca": 12,
        "max_dd_pct_for_dca": 55,
        "max_dd_pct_for_heavy": 55,
        "small_dca_usdt": 500,
        "min_meaningful_usdt": 200,
        "max_single_add_usdt": 2500,
        "heavy_min_score": 6.5,
        "prefer_small_before_heavy": True,
        "heavy_only_on_reclaim": True,
        "require_reclaim_for_dca": True,
        "profile_f": {"default": 0.65, "volatile": 0.75},
        "max_bag_pct_equity": 5,
        "exclude_grid": True,
    }
    c.update(kw)
    return c


def _ctx(**kw) -> DcaContext:
    c = DcaContext(
        symbol="BLESS/USDT",
        cash_mode="STEADY",
        fusion_size_mult=1.0,
        score=6,
        max_score=10,
        loss_pct=-52.0,
        entry_bias="neutral",
        rag_hit_count=0,
        dca_lesson_count=0,
    )
    for k, v in kw.items():
        setattr(c, k, v)
    return c


class TestEnrichFromContext(unittest.TestCase):
    def test_maps_facts_and_memory(self):
        ctx = _ctx(
            entry_bias="soft_block",
            fact_unlock=True,
            fact_hard_negative=False,
            rag_hit_count=3,
            dca_lesson_count=2,
            dca_lesson_summary="prior dca hurt",
            fact_summary="unlock soon",
            fact_event_count=1,
        )
        out = enrich_candidate_from_context(_row(), ctx)
        self.assertEqual(out["entry_bias"], "soft_block")
        self.assertTrue(out["unlock_risk"])
        self.assertEqual(out["rag_hit_count"], 3)
        self.assertEqual(out["dca_lesson_count"], 2)
        self.assertIn("context", out)


class TestDeepAnalyze(unittest.TestCase):
    def test_policy_skip_blocks_size(self):
        ctx = _ctx(block_buys=True, cash_mode="HARVEST", fusion_size_mult=0.5)
        with patch(
            "services.dca_sniper.deep_analysis._build_context", return_value=ctx
        ), patch(
            "strategies.dca_policy.emit_dca_policy_audit", return_value="ok"
        ):
            r = deep_analyze_candidate(
                _row(),
                {"spendable_dca": 2000, "equity": 100000, "cash_mode": "HARVEST"},
                _cfg(),
            )
        self.assertTrue(r.policy_skip or r.usdt == 0)
        self.assertEqual(r.usdt, 0.0)
        self.assertTrue(
            any("policy_skip" in h or h == "policy_skip" for h in r.hard_fail)
            or r.size_reason == "policy_skip"
        )
        self.assertTrue(r.deep)
        self.assertIn("policy_reasons", r.checklist)

    def test_fact_hard_negative_hard_fail_or_skip(self):
        ctx = _ctx(fact_hard_negative=True, fact_unlock=True)
        with patch(
            "services.dca_sniper.deep_analysis._build_context", return_value=ctx
        ), patch(
            "strategies.dca_policy.emit_dca_policy_audit", return_value="ok"
        ):
            r = deep_analyze_candidate(
                _row(),
                {"spendable_dca": 2000, "equity": 100000},
                _cfg(),
            )
        # facts layer hard fail → score 0 hard_fail, and/or policy skip
        self.assertTrue(
            r.usdt == 0
            or r.score == 0
            or any("facts" in h or "policy" in h for h in r.hard_fail)
        )
        self.assertTrue(r.enriched_row.get("unlock_risk") or r.enriched_row.get("hard_negative"))

    def test_soft_block_fills_memory_reason(self):
        ctx = _ctx(entry_bias="soft_block", rag_hit_count=2)
        with patch(
            "services.dca_sniper.deep_analysis._build_context", return_value=ctx
        ), patch(
            "strategies.dca_policy.emit_dca_policy_audit", return_value="ok"
        ):
            r = deep_analyze_candidate(
                _row(reclaim_ok=True, free_fall=False, loss_pct=-30),
                {"spendable_dca": 3000, "equity": 100000},
                _cfg(),
            )
        mem = (r.checklist or {}).get("memory") or {}
        self.assertIn("soft_block", str(mem.get("reason") or ""))
        self.assertIn("rag_hits", str(mem.get("reason") or ""))

    def test_policy_mult_scales_size(self):
        ctx = _ctx(cash_mode="DEPLOY", fusion_size_mult=1.2, entry_bias="prefer")
        base_usdt = 500.0

        def fake_size(row, analysis, cash, cfg):
            return base_usdt, "DCA_SMALL"

        with patch(
            "services.dca_sniper.deep_analysis._build_context", return_value=ctx
        ), patch(
            "strategies.dca_policy.emit_dca_policy_audit", return_value="ok"
        ), patch(
            "strategies.dca_policy.evaluate_dca_policy",
            return_value=DcaPolicyResult(size_mult=1.35, skip=False, reason_codes=("deploy_boost",)),
        ):
            r = deep_analyze_candidate(
                _row(reclaim_ok=True, loss_pct=-30),
                {"spendable_dca": 5000, "equity": 100000},
                _cfg(),
                size_fn=fake_size,
            )
        self.assertGreater(r.usdt, base_usdt)
        self.assertAlmostEqual(r.usdt, round(base_usdt * 1.35, 2))
        self.assertEqual(r.checklist.get("policy_mult"), 1.35)

    def test_deep_disabled_path_in_engine(self):
        rows = [_row(reclaim_ok=None, loss_pct=-30)]
        cash = {"spendable_dca": 2000, "equity": 100000}
        with patch(
            "services.dca_sniper.deep_analysis.deep_analyze_candidate"
        ) as deep_mock:
            views = _as_candidate_views(rows, cash, _cfg(deep_analysis_enabled=False))
        deep_mock.assert_not_called()
        self.assertEqual(len(views), 1)
        self.assertFalse((views[0].checklist or {}).get("deep"))

    def test_deep_enabled_calls_deep(self):
        rows = [_row()]
        cash = {"spendable_dca": 2000, "equity": 100000}
        fake = MagicMock()
        fake.score = 5.0
        fake.hard_fail = []
        fake.usdt = 500.0
        fake.size_reason = "DCA_SMALL"
        fake.checklist = {"deep": True, "memory": {"reason": "entry_bias=neutral,rag_hits=1"}}
        fake.enriched_row = _row()
        fake.policy_skip = False
        with patch(
            "services.dca_sniper.deep_analysis.deep_analyze_candidate", return_value=fake
        ):
            views = _as_candidate_views(rows, cash, _cfg(deep_analysis_enabled=True))
        self.assertEqual(len(views), 1)
        self.assertEqual(views[0].usdt_suggest, 500.0)
        self.assertTrue(views[0].checklist.get("deep"))


class TestChecklistMemoryFacts(unittest.TestCase):
    def test_rag_boosts_memory_score(self):
        from services.dca_sniper.checklist import analyze_candidate

        a = analyze_candidate(
            _row(entry_bias="neutral", rag_hit_count=0, loss_pct=-30, notional=2000),
            {"spendable_dca": 1000},
        )
        b = analyze_candidate(
            _row(entry_bias="neutral", rag_hit_count=4, loss_pct=-30, notional=2000),
            {"spendable_dca": 1000},
        )
        ma = (a["checklist"] or {}).get("memory") or {}
        mb = (b["checklist"] or {}).get("memory") or {}
        self.assertGreater(float(mb.get("score") or 0), float(ma.get("score") or 0))
        self.assertIn("rag_hits", mb.get("reason") or "")


class TestContextQuality(unittest.TestCase):
    def test_thin_blocks_heavy(self):
        from services.dca_sniper.quality import apply_quality_to_size, context_quality

        thin_cand = _row(entry_bias="neutral")  # almost no signals
        q = context_quality(thin_cand, {}, min_signals=3)
        self.assertTrue(q["thin"])
        usdt, reason, extra = apply_quality_to_size(
            usdt=1500,
            size_reason="DCA_HEAVY",
            quality=q,
            cfg=_cfg(deep_require_context_for_heavy=True, deep_allow_small_if_thin=True),
        )
        self.assertLessEqual(usdt, 500)
        self.assertIn("thin", reason.lower())

    def test_rich_keeps_heavy(self):
        from services.dca_sniper.quality import apply_quality_to_size, context_quality

        rich = _row(
            entry_bias="prefer",
            rag_hit_count=2,
            dca_lesson_count=1,
            fact_event_count=1,
            fact_summary="ok",
            reclaim_ok=True,
            free_fall=False,
            rsi=30,
            funding_rate_pct=0.01,
            cash_mode="STEADY",
        )
        q = context_quality(rich, {}, min_signals=3)
        self.assertFalse(q["thin"])
        usdt, reason, extra = apply_quality_to_size(
            usdt=1500,
            size_reason="DCA_HEAVY",
            quality=q,
            cfg=_cfg(deep_require_context_for_heavy=True),
        )
        self.assertEqual(usdt, 1500)
        self.assertEqual(reason, "DCA_HEAVY")
        self.assertEqual(extra, [])


class TestMultiTfStructure(unittest.TestCase):
    def test_aggregate_free_fall_any(self):
        from services.dca_sniper import structure as st

        with patch.object(
            st,
            "structure_flags_for_symbol",
            side_effect=[
                {"free_fall": False, "reclaim_ok": False, "structure_ok": False},
                {"free_fall": True, "reclaim_ok": False, "structure_ok": False},
                {"free_fall": False, "reclaim_ok": True, "structure_ok": True},
            ],
        ):
            m = st.structure_flags_multi_tf("X/USDT", ("15m", "1h", "4h"))
        self.assertTrue(m["free_fall"])
        self.assertTrue(m["reclaim_ok"])  # 4h reclaim
        self.assertFalse(m["structure_ok"])  # free_fall wins
        self.assertIn("1h", m["structure_by_tf"])


class TestDeepQualityGateIntegration(unittest.TestCase):
    def test_thin_context_demotes_heavy_in_deep_pass(self):
        # Almost no context channels → thin → demote heavy
        ctx = _ctx(
            entry_bias="neutral",
            cash_mode="",
            size_bias=1.0,
            rag_hit_count=0,
            dca_lesson_count=0,
            fact_event_count=0,
            fact_summary="",
        )

        def fake_size(row, analysis, cash, cfg):
            return 1800.0, "DCA_HEAVY"

        with patch(
            "services.dca_sniper.deep_analysis._build_context", return_value=ctx
        ), patch(
            # no structure either
            "services.dca_sniper.deep_analysis._enrich_structure_multi_tf",
            side_effect=lambda r, c: {
                **r,
                "reclaim_ok": None,
                "free_fall": None,
                "structure_ok": None,
            },
        ), patch(
            "services.dca_sniper.deep_analysis._enrich_social_soft",
            side_effect=lambda r: r,
        ), patch(
            "strategies.dca_policy.emit_dca_policy_audit", return_value="ok"
        ), patch(
            "strategies.dca_policy.evaluate_dca_policy",
            return_value=DcaPolicyResult(size_mult=1.0, skip=False, reason_codes=("steady",)),
        ):
            r = deep_analyze_candidate(
                _row(
                    reclaim_ok=None,
                    free_fall=None,
                    loss_pct=-30,
                    rsi=None,
                    atr_pct=None,
                    funding_rate_pct=None,
                    cash_mode="",
                ),
                {"spendable_dca": 5000, "equity": 100000, "cash_mode": ""},
                _cfg(
                    deep_min_context_signals=3,
                    deep_require_context_for_heavy=True,
                    deep_allow_small_if_thin=True,
                ),
                size_fn=fake_size,
            )
        self.assertTrue(r.quality.get("thin"), r.quality)
        self.assertNotEqual(r.size_reason, "DCA_HEAVY")
        self.assertIn("quality", r.checklist)


if __name__ == "__main__":
    unittest.main()
