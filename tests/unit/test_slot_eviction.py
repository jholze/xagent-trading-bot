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
    fraction_to_free_full_slot,
    memory_keep_score,
    plan_slot_eviction,
    score_entry_demand,
    would_be_tail_after_sell,
)
from risk.slot_eviction_rag import enrich_keeps_with_rag
from risk.slot_eviction_runtime import (
    execute_eviction_sell,
    resolve_spendable_ok_for_entry,
    try_slot_eviction_on_max_open,
)
from strategies.exit_attribution import resolve_exit_source
from strategies.sell_rotation_policy import is_tail_position


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


class TestFractionFreesFullSlot(unittest.TestCase):
    def test_mid_notional_partial_reaches_tail_sold(self):
        # $2000 bag, 0% sold → need sold>=0.55 → frac >= 0.55
        frac, action, already = fraction_to_free_full_slot(
            sold_percent=0.0,
            notional_usdt=2000.0,
            tail_sold_pct=0.55,
            tail_notional_usdt=800.0,
        )
        self.assertFalse(already)
        self.assertGreaterEqual(frac, 0.55)
        self.assertTrue(
            would_be_tail_after_sell(
                sold_percent=0.0,
                notional_usdt=2000.0,
                sell_fraction=frac,
                tail_sold_pct=0.55,
                tail_notional_usdt=800.0,
            )
        )
        # Align with production is_tail_position rules
        new_sold = 0.0 + (1.0 - 0.0) * frac
        new_notional = 2000.0 * (1.0 - frac)
        pos = {
            "amount": max(1e-9, 1.0 * (1.0 - frac)),
            "average_entry": new_notional / max(1e-9, 1.0 * (1.0 - frac)) if frac < 1 else 1.0,
            "sold_percent": new_sold,
        }
        # if full close, not open
        if frac >= 0.99:
            self.assertTrue(True)
        else:
            self.assertTrue(
                is_tail_position(
                    pos,
                    {
                        "tail_exempt_sold_pct": 0.55,
                        "tail_exempt_notional_usdt": 800.0,
                    },
                )
            )

    def test_large_bag_plan_sell_fraction_frees_slot(self):
        """plan_slot_eviction sizes action so mid/large bag becomes tail."""
        demand = score_entry_demand(
            symbol="BANK/USDT",
            source="entry_sensor_15m",
            free_full_slots=0,
            spike_multiple=5.0,
            risk_config=_cfg(
                tail_target_sold_pct=0.55,
                tail_target_max_notional_usdt=800,
            ),
        )
        cands = [
            _cand(
                "BBB/USDT",
                keep_profile=0.3,
                keep_rag=0.3,
                gain=2.0,
                notional=2500.0,
                sold=0.0,
            ),
            VictimCandidate(
                **{
                    **_cand("BANK/USDT", keep_profile=0.7, keep_rag=0.7).to_dict(),
                    "veto": "entry_self",
                }
            ),
        ]
        plan = plan_slot_eviction(
            demand=demand,
            candidates=cands,
            risk_config=_cfg(
                tail_target_sold_pct=0.55,
                tail_target_max_notional_usdt=800,
            ),
        )
        self.assertTrue(plan.ok, plan.veto_reason)
        self.assertEqual(plan.victim_symbol, "BBB/USDT")
        self.assertGreaterEqual(plan.sell_fraction, 0.55)
        self.assertTrue(
            would_be_tail_after_sell(
                sold_percent=0.0,
                notional_usdt=2500.0,
                sell_fraction=plan.sell_fraction,
                tail_sold_pct=0.55,
                tail_notional_usdt=800.0,
            )
        )
        # fixed 0.40 would leave $1500 full-slot — prove we beat that
        self.assertFalse(
            would_be_tail_after_sell(
                sold_percent=0.0,
                notional_usdt=2500.0,
                sell_fraction=0.40,
                tail_sold_pct=0.55,
                tail_notional_usdt=800.0,
            )
        )


class TestSpendableGate(unittest.TestCase):
    def test_spendable_override_false_blocks_demand(self):
        d = score_entry_demand(
            symbol="BANK/USDT",
            source="entry_sensor_15m",
            free_full_slots=0,
            spike_multiple=5.0,
            spendable_ok=False,
            risk_config=_cfg(),
        )
        self.assertFalse(d.passed)
        self.assertIn("spendable", d.must_fail_reasons)

    def test_resolve_spendable_uses_risk_manager(self):
        from core.models import TradeOrder

        order = TradeOrder(
            type="BUY", symbol="BANK/USDT", price=1.0, amount=0, usdt_amount=500, signal="BUY"
        )
        rm = MagicMock()
        rm._portfolio_equity.return_value = 100_000.0
        rm._spendable_usdt.return_value = 50.0  # below min_trade 100
        ok = resolve_spendable_ok_for_entry(
            order=order,
            risk_manager=rm,
            risk_config={"min_trade_usdt": 100, "slot_eviction": {"require_spendable_for_entry": True}},
        )
        self.assertFalse(ok)
        rm._spendable_usdt.return_value = 2000.0
        ok2 = resolve_spendable_ok_for_entry(
            order=order,
            risk_manager=rm,
            risk_config={"min_trade_usdt": 100, "slot_eviction": {"require_spendable_for_entry": True}},
        )
        self.assertTrue(ok2)


class TestRiskMaxOpenIntegration(unittest.TestCase):
    def test_max_open_invokes_eviction_hook_with_risk_manager(self):
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
            "require_spendable_for_entry": True,
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
        # risk_manager=self must be passed for spendable gate
        kwargs = hook.call_args.kwargs
        self.assertIs(kwargs.get("risk_manager"), rm)

    def test_mode_off_no_sell_path(self):
        from core.models import TradeOrder

        order = TradeOrder(
            type="BUY",
            symbol="BANK/USDT",
            price=1.0,
            amount=0,
            usdt_amount=500,
            signal="BUY",
            entry_15m_vol_ratio=5.5,
        )
        plan, suffix = try_slot_eviction_on_max_open(
            order=order,
            source="entry_sensor_15m",
            free_full_slots=0,
            config=None,
            risk_config=_cfg(mode="off"),
            spendable_ok=True,
        )
        self.assertIsNone(plan)
        self.assertEqual(suffix, "")

    def test_live_execute_uses_real_plan_fraction(self):
        """execute_eviction_sell consumes real EvictionPlan (not a mocked plan builder)."""
        from core.models import TradeOrder, TradeResult

        demand = score_entry_demand(
            symbol="BANK/USDT",
            source="entry_sensor_15m",
            free_full_slots=0,
            spike_multiple=5.0,
            risk_config=_cfg(mode="live"),
        )
        cands = [
            _cand("BBB/USDT", keep_profile=0.3, notional=2000.0, sold=0.0, gain=2.0),
            VictimCandidate(
                **{**_cand("BANK/USDT", keep_profile=0.7).to_dict(), "veto": "entry_self"}
            ),
        ]
        plan = plan_slot_eviction(
            demand=demand, candidates=cands, risk_config=_cfg(mode="live")
        )
        self.assertTrue(plan.ok)
        self.assertGreaterEqual(plan.sell_fraction, 0.55)

        mock_svc = MagicMock()
        mock_svc.execute_order.return_value = TradeResult(
            executed=True, order_type="SELL", symbol="BBB/USDT", message="ok"
        )
        mock_svc.market = None
        with patch(
            "strategies.positions.get_position",
            return_value={"amount": 100.0, "average_entry": 20.0, "mark_price": 20.0},
        ), patch(
            "risk.slot_eviction_runtime.note_eviction_executed"
        ), patch(
            "risk.slot_eviction_runtime.set_pending_entry"
        ):
            res = execute_eviction_sell(plan, trading=mock_svc)
        self.assertTrue(res.get("ok"), res)
        call_order = mock_svc.execute_order.call_args[0][0]
        self.assertEqual(call_order.type, "SELL")
        self.assertEqual(call_order.exit_source, EXIT_SOURCE_SLOT_EVICT)
        # amount = 100 * sell_fraction from real plan
        self.assertAlmostEqual(
            float(call_order.amount), 100.0 * float(plan.sell_fraction), places=4
        )


class TestEvidenceDelta(unittest.TestCase):
    def test_empty_hits_zero(self):
        self.assertEqual(evidence_delta_from_hits([]), 0.0)
        self.assertEqual(evidence_delta_from_hits(None), 0.0)


if __name__ == "__main__":
    unittest.main()
