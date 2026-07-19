"""#111 Slot eviction — pure plan, memory rank, RAG apply/fail-open, hard gates."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from risk.slot_eviction import (
    EXIT_SOURCE_SLOT_EVICT,
    VictimCandidate,
    apply_rag_keep,
    evidence_delta_from_hits,
    eviction_mode,
    format_eviction_reject_suffix,
    memory_keep_score,
    plan_slot_eviction,
    score_entry_demand,
)
from risk.slot_eviction_rag import enrich_keeps_with_rag
from strategies.exit_attribution import resolve_exit_source


def _cfg(**over) -> dict:
    base = {
        "enabled": True,
        "mode": "live",
        "min_entry_score": 4,
        "min_entry_score_shadow": 3,
        "min_victim_score": 0.15,
        "min_hold_hours": 3,
        "protect_peak_gain_pct": 12,
        "max_evict_notional_usdt": 8000,
        "prefer_reduce_to_tail": True,
        "require_sensor_source": True,
        "skip_if_warmup": True,
        "skip_if_block_buys": True,
        "skip_if_crash": True,
        "memory": {
            "min_entry_keep_edge": 0.12,
            "prefer_keep_floor": 0.7,
            "prefer_is_hard_keep": True,
            "missing_profile_keep": 0.5,
            "min_samples_for_win_rate": 3,
        },
        "rag": {
            "mode": "retrieve",
            "apply_to_plan": True,
            "evidence_weight": 0.25,
        },
        "weights": {"memory": 0.55, "idle": 0.2, "pnl_flat": 0.1, "tail_ready": 0.15},
        "sources": ["entry_sensor_15m", "vol_spike_15m"],
    }
    base.update(over)
    return {"slot_eviction": base}


def _cand(
    symbol: str,
    *,
    keep_profile: float,
    keep_rag: float | None = None,
    gain: float = 3.0,
    trail: bool = False,
    prefer: bool = False,
    age: float = 48.0,
    peak: float = 3.0,
    idle: float = 40.0,
    notional: float = 1000.0,
    sold: float = 0.0,
    rot: bool = True,
) -> VictimCandidate:
    kr = keep_rag if keep_rag is not None else keep_profile
    return VictimCandidate(
        symbol=symbol,
        timeframe="4h",
        gain_pct=gain,
        peak_gain_pct=peak,
        idle_hours=idle,
        sold_percent=sold,
        notional_usdt=notional,
        amount=10.0,
        price=1.0,
        keep_profile=keep_profile,
        keep_rag=kr,
        keep_final=kr,  # staging apply_to_plan style; plan re-picks
        trail_armed=trail,
        rotation_eligible=rot if gain >= 0 else rot,
        prefer=prefer,
        age_hours=age,
        class_name="A" if gain >= 0 else "B",
    )


class TestEntryDemand(unittest.TestCase):
    def test_free_slots_positive_no_plan_need(self):
        d = score_entry_demand(
            symbol="BANK/USDT",
            source="entry_sensor_15m",
            free_full_slots=2,
            spike_multiple=5.0,
            risk_config=_cfg(),
        )
        self.assertFalse(d.passed)
        self.assertIn("free_slots_available", d.must_fail_reasons)

    def test_sensor_spike_passes(self):
        d = score_entry_demand(
            symbol="BANK/USDT",
            source="entry_sensor_15m",
            free_full_slots=0,
            spike_multiple=5.0,
            risk_config=_cfg(),
        )
        self.assertTrue(d.passed)
        self.assertGreaterEqual(d.score, 4.0)

    def test_soft_block_must_fail(self):
        d = score_entry_demand(
            symbol="BANK/USDT",
            source="entry_sensor_15m",
            free_full_slots=0,
            spike_multiple=5.0,
            soft_block=True,
            risk_config=_cfg(),
        )
        self.assertFalse(d.passed)
        self.assertIn("soft_block", d.must_fail_reasons)

    def test_block_buys_must_fail(self):
        d = score_entry_demand(
            symbol="BANK/USDT",
            source="entry_sensor_15m",
            free_full_slots=0,
            spike_multiple=5.0,
            block_buys=True,
            risk_config=_cfg(),
        )
        self.assertFalse(d.passed)


class TestKeepScore(unittest.TestCase):
    def test_prefer_higher_than_soft_block(self):
        pref = SimpleNamespace(
            entry_bias="prefer",
            size_bias=1.1,
            win_rate=0.7,
            sells_30d=10,
            risk_score=0.3,
            total_pnl_usdt=200.0,
            features={},
        )
        weak = SimpleNamespace(
            entry_bias="soft_block",
            size_bias=0.7,
            win_rate=0.2,
            sells_30d=10,
            risk_score=0.8,
            total_pnl_usdt=-800.0,
            features={"structure_risk": True},
        )
        self.assertGreater(
            memory_keep_score(pref, risk_config=_cfg()),
            memory_keep_score(weak, risk_config=_cfg()),
        )

    def test_missing_profile_neutral(self):
        k = memory_keep_score(None, risk_config=_cfg())
        self.assertAlmostEqual(k, 0.5, places=2)


class TestPlanHardGates(unittest.TestCase):
    def _demand(self):
        return score_entry_demand(
            symbol="BANK/USDT",
            source="entry_sensor_15m",
            free_full_slots=0,
            spike_multiple=5.0,
            risk_config=_cfg(),
        )

    def test_two_greens_weak_keep_is_victim(self):
        demand = self._demand()
        cands = [
            _cand("AAA/USDT", keep_profile=0.82, keep_rag=0.82, gain=5.0, prefer=True),
            _cand("BBB/USDT", keep_profile=0.32, keep_rag=0.32, gain=4.0),
            # entry keep high enough for swap
            _cand("BANK/USDT", keep_profile=0.65, keep_rag=0.65, gain=0.0),
        ]
        # mark entry self for swap gate
        entry = cands[2]
        cands[2] = VictimCandidate(**{**entry.to_dict(), "veto": "entry_self"})
        plan = plan_slot_eviction(
            demand=demand, candidates=cands, risk_config=_cfg()
        )
        self.assertTrue(plan.ok)
        self.assertEqual(plan.victim_symbol, "BBB/USDT")
        self.assertEqual(plan.exit_source, EXIT_SOURCE_SLOT_EVICT)
        self.assertEqual(plan.profile_victim, "BBB/USDT")

    def test_trail_armed_never_victim(self):
        demand = self._demand()
        cands = [
            _cand("CCC/USDT", keep_profile=0.2, keep_rag=0.2, gain=8.0, trail=True, peak=20.0),
            _cand("BANK/USDT", keep_profile=0.7, keep_rag=0.7, gain=0.0),
        ]
        cands[1] = VictimCandidate(**{**cands[1].to_dict(), "veto": "entry_self"})
        plan = plan_slot_eviction(
            demand=demand, candidates=cands, risk_config=_cfg()
        )
        self.assertFalse(plan.ok)
        self.assertEqual(plan.veto_reason, "no_candidate")

    def test_swap_edge_fail(self):
        demand = self._demand()
        # both strong; entry mediocre
        cands = [
            _cand("AAA/USDT", keep_profile=0.85, keep_rag=0.85, gain=5.0, prefer=True),
            _cand("BBB/USDT", keep_profile=0.78, keep_rag=0.78, gain=4.0),
            _cand("BANK/USDT", keep_profile=0.50, keep_rag=0.50, gain=0.0),
        ]
        cands[2] = VictimCandidate(**{**cands[2].to_dict(), "veto": "entry_self"})
        plan = plan_slot_eviction(
            demand=demand, candidates=cands, risk_config=_cfg()
        )
        self.assertFalse(plan.ok)
        self.assertEqual(plan.veto_reason, "memory_swap_not_worth_it")

    def test_mode_off_no_ok(self):
        demand = self._demand()
        plan = plan_slot_eviction(
            demand=demand,
            candidates=[_cand("BBB/USDT", keep_profile=0.3, gain=2.0)],
            risk_config=_cfg(mode="off"),
        )
        self.assertFalse(plan.ok)
        self.assertEqual(plan.veto_reason, "mode_off")
        self.assertEqual(eviction_mode(_cfg(mode="shadow")), "shadow")

    def test_shadow_ok_but_format_says_shadow(self):
        demand = self._demand()
        cands = [
            _cand("BBB/USDT", keep_profile=0.3, keep_rag=0.3, gain=2.0),
            _cand("BANK/USDT", keep_profile=0.7, keep_rag=0.7, gain=0.0),
        ]
        cands[1] = VictimCandidate(**{**cands[1].to_dict(), "veto": "entry_self"})
        plan = plan_slot_eviction(
            demand=demand, candidates=cands, risk_config=_cfg(mode="shadow")
        )
        self.assertTrue(plan.ok)
        self.assertEqual(plan.mode, "shadow")
        s = format_eviction_reject_suffix(plan)
        self.assertIn("shadow", s.lower())


class TestRagApply(unittest.TestCase):
    def test_evidence_loss_lowers_keep(self):
        hits = [SimpleNamespace(text="gross_loss soft_block structure_risk", score=0.9)]
        kr, ed = apply_rag_keep(0.6, hits, evidence_weight=0.25)
        self.assertLess(kr, 0.6)
        self.assertLess(ed, 0)

    def test_retrieve_error_fail_open(self):
        kr, ed = apply_rag_keep(0.6, None, retrieve_error=True)
        self.assertEqual(kr, 0.6)
        self.assertEqual(ed, 0.0)

    def test_apply_to_plan_true_flips_victim(self):
        demand = score_entry_demand(
            symbol="BANK/USDT",
            source="entry_sensor_15m",
            free_full_slots=0,
            spike_multiple=5.0,
            risk_config=_cfg(),
        )
        # Profile prefers free BBB (lower keep_profile); RAG makes AAA weaker
        cands = [
            _cand("AAA/USDT", keep_profile=0.55, keep_rag=0.28, gain=4.0),
            _cand("BBB/USDT", keep_profile=0.50, keep_rag=0.62, gain=4.0),
            _cand("BANK/USDT", keep_profile=0.65, keep_rag=0.65, gain=0.0),
        ]
        cands[2] = VictimCandidate(**{**cands[2].to_dict(), "veto": "entry_self"})
        plan = plan_slot_eviction(
            demand=demand,
            candidates=cands,
            risk_config=_cfg(rag={"mode": "retrieve", "apply_to_plan": True, "evidence_weight": 0.25}),
        )
        self.assertTrue(plan.ok)
        self.assertEqual(plan.profile_victim, "BBB/USDT")
        self.assertEqual(plan.rag_victim, "AAA/USDT")
        self.assertEqual(plan.applied_victim, "AAA/USDT")
        self.assertEqual(plan.victim_symbol, "AAA/USDT")

    def test_apply_to_plan_false_uses_profile(self):
        demand = score_entry_demand(
            symbol="BANK/USDT",
            source="entry_sensor_15m",
            free_full_slots=0,
            spike_multiple=5.0,
            risk_config=_cfg(),
        )
        cands = [
            _cand("AAA/USDT", keep_profile=0.55, keep_rag=0.28, gain=4.0),
            _cand("BBB/USDT", keep_profile=0.50, keep_rag=0.62, gain=4.0),
            _cand("BANK/USDT", keep_profile=0.65, keep_rag=0.65, gain=0.0),
        ]
        cands[2] = VictimCandidate(**{**cands[2].to_dict(), "veto": "entry_self"})
        plan = plan_slot_eviction(
            demand=demand,
            candidates=cands,
            risk_config=_cfg(
                rag={"mode": "retrieve", "apply_to_plan": False, "evidence_weight": 0.25}
            ),
        )
        self.assertTrue(plan.ok)
        self.assertEqual(plan.victim_symbol, "BBB/USDT")
        self.assertEqual(plan.applied_victim, "BBB/USDT")

    def test_enrich_fail_open_on_retrieve_error(self):
        def boom(sym, q):
            raise RuntimeError("down")

        out = enrich_keeps_with_rag(
            ["AAA/USDT"],
            {"AAA/USDT": 0.6},
            risk_config=_cfg(),
            retrieve_fn=boom,
        )
        self.assertEqual(out["AAA/USDT"]["keep_rag"], 0.6)
        self.assertTrue(out["AAA/USDT"]["error"])

    def test_enrich_with_hits_changes_keep(self):
        def hits(sym, q):
            return [SimpleNamespace(text="gross_loss stop blowup", score=0.95)]

        out = enrich_keeps_with_rag(
            ["AAA/USDT"],
            {"AAA/USDT": 0.6},
            risk_config=_cfg(),
            retrieve_fn=hits,
        )
        self.assertLess(out["AAA/USDT"]["keep_rag"], 0.6)


class TestExitAttribution(unittest.TestCase):
    def test_slot_evict_label_preserved(self):
        self.assertEqual(
            resolve_exit_source(sell_source="slot_evict_for_entry", sources=["auto"]),
            "slot_evict_for_entry",
        )


class TestRiskMaxOpenIntegration(unittest.TestCase):
    def test_max_open_invokes_eviction_hook(self):
        from core.config import BotConfig
        from core.models import TradeOrder
        from data_manager import get_config
        from risk.risk_manager import RiskManager

        raw = dict(get_config())
        raw["trading_mode"] = "paper"
        raw["max_open_positions"] = 24
        risk = raw.setdefault("risk", {})
        risk["cash_policy"] = {"enabled": False}
        risk["position_capacity"] = {"enabled": False}
        risk["slot_eviction"] = {
            "enabled": True,
            "mode": "shadow",
            "min_entry_score": 4,
            "sources": ["entry_sensor_15m"],
            "rag": {"mode": "off", "apply_to_plan": False},
            "memory": {"min_entry_keep_edge": 0.12, "prefer_is_hard_keep": True},
        }
        risk["venue_quality"] = {"enabled": False}
        rm = RiskManager(BotConfig(raw))
        order = TradeOrder(
            type="BUY",
            symbol="BANK/USDT",
            price=1.0,
            amount=0,
            usdt_amount=500,
            signal="BUY",
            entry_15m_vol_ratio=5.5,
        )
        with patch(
            "risk.risk_manager.count_open_full_slots", return_value=24
        ), patch(
            "risk.risk_manager.get_position", return_value={"amount": 0}
        ), patch.object(rm, "_trade_cooldown_blocked", return_value=(False, "")), patch(
            "services.market_policy_fusion.get_global_market_bias",
            return_value={"block_buys": False, "size_mult": 1.0, "regime": "RISK_ON"},
        ), patch(
            "risk.slot_eviction_runtime.try_slot_eviction_on_max_open",
            return_value=(MagicMock(ok=True, mode="shadow"), " · eviction shadow would X/USDT"),
        ) as hook:
            decision = rm.evaluate(order, "4h", source="entry_sensor_15m")
        self.assertFalse(decision.approved)
        self.assertEqual(decision.code, "max_open_positions")
        self.assertIn("eviction", decision.message.lower())
        hook.assert_called_once()


class TestEvidenceDelta(unittest.TestCase):
    def test_empty_hits_zero(self):
        self.assertEqual(evidence_delta_from_hits([]), 0.0)
        self.assertEqual(evidence_delta_from_hits(None), 0.0)


if __name__ == "__main__":
    unittest.main()
