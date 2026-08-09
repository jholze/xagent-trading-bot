"""Santiment Pro enrichment for DCA sniper deep analysis."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from services.dca_sniper.santiment_enrich import (
    apply_santiment_size,
    apply_santiment_to_candidate,
    build_santiment_enrichment,
    resolve_santiment_slug,
    score_asset_signals,
)
from services.dca_sniper.quality import context_signal_flags


class TestSlugMap(unittest.TestCase):
    def test_common_bases(self):
        self.assertEqual(resolve_santiment_slug("BTC/USDT"), "bitcoin")
        self.assertEqual(resolve_santiment_slug("eth"), "ethereum")
        self.assertEqual(resolve_santiment_slug("SOL"), "solana")


class TestScoreAssetSignals(unittest.TestCase):
    def test_empty_neutral(self):
        s = score_asset_signals({})
        self.assertEqual(s["size_mult"], 1.0)
        self.assertFalse(s["caution"])
        self.assertEqual(s["hints"], [])

    def test_daa_decline_cuts_size(self):
        s = score_asset_signals({"daa_delta_1d": -0.25})
        self.assertLess(s["size_mult"], 1.0)
        self.assertTrue(s["onchain_weak"])
        self.assertIn("daa_declining", s["hints"])

    def test_social_spike_caution(self):
        s = score_asset_signals({"social_volume_delta_1d": 0.5})
        self.assertTrue(s["social_hot"])
        self.assertTrue(s["caution"])
        self.assertLess(s["size_mult"], 1.0)

    def test_exchange_inflow_dominant(self):
        s = score_asset_signals(
            {"exchange_inflow": 1000.0, "exchange_outflow": 400.0}
        )
        self.assertTrue(s["exchange_distribution"])
        self.assertLess(s["size_mult"], 0.9)

    def test_high_vol_cuts(self):
        s = score_asset_signals({"vol_1d": 0.09})
        self.assertTrue(s["high_vol"])
        self.assertLess(s["size_mult"], 0.8)

    def test_research_half_weight_no_hard_caution(self):
        """Lagged Pro metrics must not set hard social_hot/onchain_weak alone."""
        s = score_asset_signals(
            {
                "research_social_volume_delta_1d": 0.6,
                "research_daa_delta_1d": -0.3,
            }
        )
        self.assertTrue(s["used_research"])
        self.assertFalse(s["social_hot"])
        self.assertFalse(s["onchain_weak"])
        self.assertLess(s["size_mult"], 1.0)
        self.assertTrue(any("research" in h for h in s["hints"]))

    def test_mvrv_cheap_bump(self):
        s = score_asset_signals({"mvrv": 0.8})
        self.assertGreater(s["size_mult"], 1.0)
        self.assertIn("mvrv_cheap", s["hints"])


class TestApplySize(unittest.TestCase):
    def test_block_buys_zeroes(self):
        pack = {
            "social_block": True,
            "combined_size_mult": 0.5,
            "asset": {"score": {}},
        }
        usdt, reason, extra = apply_santiment_size(
            1000, "DCA_HEAVY", pack, cfg={"deep_santiment_block_buys": True}
        )
        self.assertEqual(usdt, 0.0)
        self.assertEqual(reason, "santiment_block_buys")
        self.assertIn("santiment_block_buys", extra)

    def test_mult_scales_size(self):
        pack = {
            "social_block": False,
            "combined_size_mult": 0.7,
            "asset": {"score": {}},
        }
        usdt, reason, extra = apply_santiment_size(
            1000, "DCA_HEAVY", pack, cfg={"min_meaningful_usdt": 200}
        )
        self.assertAlmostEqual(usdt, 700.0)
        self.assertTrue(any("santiment_mult" in e for e in extra))

    def test_flow_demotes_heavy(self):
        pack = {
            "social_block": False,
            "combined_size_mult": 1.0,
            "asset": {
                "score": {
                    "exchange_distribution": True,
                    "size_mult": 0.75,
                }
            },
        }
        usdt, reason, extra = apply_santiment_size(
            2000,
            "DCA_HEAVY",
            pack,
            cfg={"small_dca_usdt": 500, "min_meaningful_usdt": 200},
        )
        self.assertEqual(usdt, 500.0)
        self.assertIn("santiment_flow", reason)


class TestBuildEnrichment(unittest.TestCase):
    def test_global_block_without_asset_key(self):
        fake_pol = {
            "active": True,
            "fresh": True,
            "regime": "CRASH",
            "size_mult": 0.0,
            "block_buys": True,
            "apply_size_mult": True,
            "rationale": "test crash",
        }
        with patch(
            "services.dca_sniper.santiment_enrich.get_global_santiment",
            return_value=fake_pol,
        ), patch(
            "services.dca_sniper.santiment_enrich.get_global_snapshot",
            return_value={"fresh": True, "as_of": "2026-08-09T12:00:00Z", "regime": "CRASH"},
        ), patch(
            "services.dca_sniper.santiment_enrich.fetch_asset_santiment",
            return_value={
                "available": False,
                "reason": "no_api_key",
                "features": {},
                "meta": {},
            },
        ):
            pack = build_santiment_enrichment(
                "BTC/USDT", fetch_asset=True, config_raw={}
            )
        self.assertTrue(pack["social_block"])
        self.assertEqual(pack["regime"], "CRASH")
        self.assertEqual(pack["combined_size_mult"], 0.0)  # CRASH keeps zero

    def test_asset_mult_combined(self):
        fake_pol = {
            "active": True,
            "fresh": True,
            "regime": "NEUTRAL",
            "size_mult": 0.85,
            "block_buys": False,
            "apply_size_mult": True,
            "rationale": "neutral",
        }
        with patch(
            "services.dca_sniper.santiment_enrich.get_global_santiment",
            return_value=fake_pol,
        ), patch(
            "services.dca_sniper.santiment_enrich.get_global_snapshot",
            return_value=None,
        ), patch(
            "services.dca_sniper.santiment_enrich.fetch_asset_santiment",
            return_value={
                "available": True,
                "features": {"daa_delta_1d": -0.2},
                "meta": {"fresh": True, "metrics_ok": ["daa"]},
            },
        ):
            pack = build_santiment_enrichment("ETH/USDT", fetch_asset=True)
        self.assertLess(pack["combined_size_mult"], 0.85)
        self.assertTrue(pack["asset"]["score"]["onchain_weak"])


class TestCandidateMerge(unittest.TestCase):
    def test_apply_sets_flags(self):
        pack = {
            "social_block": True,
            "social_caution": True,
            "regime": "RISK_OFF",
            "snapshot_fresh": True,
            "asset": {
                "available": True,
                "score": {
                    "social_hot": True,
                    "exchange_distribution": True,
                },
            },
        }
        out = apply_santiment_to_candidate({"symbol": "X/USDT"}, pack)
        self.assertTrue(out["block_buys"])
        self.assertTrue(out["social_block"])
        self.assertTrue(out["social_noise"])
        self.assertTrue(out["santiment_fresh"])
        self.assertEqual(out["santiment_regime"], "RISK_OFF")
        self.assertTrue(out["santiment_exchange_distribution"])


class TestQualitySantiment(unittest.TestCase):
    def test_has_santiment_flag(self):
        flags = context_signal_flags(
            {
                "santiment": {"regime": "NEUTRAL", "snapshot_fresh": True},
                "santiment_fresh": True,
            }
        )
        self.assertTrue(flags["has_santiment"])


class TestDeepWire(unittest.TestCase):
    def test_deep_applies_santiment_block(self):
        from services.dca_sniper.deep_analysis import deep_analyze_candidate
        from strategies.dca_policy import DcaContext

        ctx = DcaContext(
            symbol="BLESS/USDT",
            cash_mode="STEADY",
            fusion_size_mult=1.0,
            score=7,
            max_score=10,
            loss_pct=-40.0,
            entry_bias="neutral",
            rag_hit_count=2,
            dca_lesson_count=1,
            fact_event_count=1,
            fact_summary="ok",
        )
        crash_pack = {
            "global": {"active": True, "fresh": True, "regime": "CRASH", "block_buys": True},
            "snapshot_fresh": True,
            "regime": "CRASH",
            "global_size_mult": 0.0,
            "asset": {"available": False, "score": {}, "features": {}, "meta": {}},
            "asset_size_mult": 1.0,
            "combined_size_mult": 0.35,
            "social_block": True,
            "social_caution": True,
            "rationale": "crash test",
            "scores": None,
            "features_global": None,
        }
        row = {
            "symbol": "BLESS/USDT",
            "timeframe": "1h",
            "average_entry": 0.027,
            "amount": 100000,
            "mark": 0.016,
            "loss_pct": -40.0,
            "notional": 1600,
            "dca_rounds": 1,
            "max_rounds": 4,
            "recovery_hold": False,
            "sniper_focus": False,
            "strategy_profile": "volatile_altcoin",
            "reclaim_ok": True,
            "free_fall": False,
            "rsi": 40,
            "atr_pct": 6,
            "funding_rate_pct": 0.001,
            "spendable_dca": 3000,
        }
        cfg = {
            "deep_analysis_enabled": True,
            "deep_include_rag": True,
            "deep_apply_policy": True,
            "deep_policy_shadow": False,
            "deep_santiment_enabled": True,
            "deep_santiment_block_buys": True,
            "deep_gather_evidence": False,
            "deep_structure_multi_tf": False,
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
            "deep_min_context_signals": 2,
            "deep_require_context_for_heavy": True,
            "deep_allow_small_if_thin": True,
        }

        def size_fn(cand, analysis, cash, c):
            return 1500.0, "DCA_HEAVY"

        with patch(
            "services.dca_sniper.deep_analysis._build_context", return_value=ctx
        ), patch(
            "services.dca_sniper.deep_analysis.build_santiment_enrichment",
            return_value=crash_pack,
        ), patch(
            "strategies.dca_policy.emit_dca_policy_audit", return_value="ok"
        ), patch(
            "strategies.dca_policy.evaluate_dca_policy",
            return_value=type(
                "R",
                (),
                {
                    "skip": False,
                    "size_mult": 1.0,
                    "reason_codes": [],
                },
            )(),
        ):
            r = deep_analyze_candidate(
                row,
                {"spendable_dca": 3000, "equity": 100000, "cash_mode": "STEADY"},
                cfg,
                size_fn=size_fn,
            )
        self.assertEqual(r.usdt, 0.0)
        self.assertEqual(r.size_reason, "santiment_block_buys")
        self.assertIsNotNone(r.checklist.get("santiment"))
        self.assertEqual(r.checklist["santiment"]["regime"], "CRASH")


if __name__ == "__main__":
    unittest.main()
