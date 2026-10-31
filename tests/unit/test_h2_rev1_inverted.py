"""H2 / #735 Spec Revision 1 (Variant B fail-closed, decided by Kay). New file; replaces, under Rev1, the Variant-A pins
  E12H2  test_h2_port_e2.py::TestE12H2NoAttributionAtALossIsHard          (no attribution at a loss -> FULL)
  E21H2  test_h2_port_e3.py::TestE21H2StopLossSellSourceAtLossEscalates   (sell_source stop_loss, no flag -> FULL)
  E27H2  test_h2_port_e4.py::TestE27H2StopLossPartialAtLossEndToEnd       (flagless auto stop_loss via execute_order -> FULL)
  AC3-s2 test_h2_ws_partial_min_size.py::TestAC3CyclePartialNoOrderFlag[...no_flag_exit_source_stop_loss]
  E18H2[auto-*] (test_h2_port_e2.py), E22 control_flagged_stop_loss_auto_escalates, E23H2, E25H2 (test_h2_port_e3.py)
which the Rev1 PR deletes (run deselected). Rev1 rule: escalation only with an explicit hard-stop origin (WS stop path
exit_ws, or cycle evaluate_long_hard_stop). Otherwise the partial_sell_guard rejection stands: no FULL, no upgrade line.
Each inverted case: cycle (or execute_order) AND risk direct (locked evaluate), both tenants, at a loss (HBAR_PX < entry).
Base 930792a3 + mutants/proto_fix_h2_rev1.py. Killer mutants: hr1_fallback_counts_as_hard, hr2_mark_without_long_hard_stop.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from core.models import TradeOrder
from h2_port_helpers import (  # noqa: F401  (autouse fixture imported by name)
    HBAR_PX, TENANTS, TF, _analysis, _filled_sells, _held, _key_missing, _logs_lvl, _paper, _params,
    _prod_floors_and_clean_books, _rm, _seed_t, _tenant,
)
from test_h2_port_e2 import NONHARD
from test_h2_port_e3 import AMT, _assert_blocked, _fulls, _orch, _run, _upgrades

SHAPES = {  # analysis shapes without long_hard_stop whose exit_source still resolves to stop_loss
    "E12H2_no_attribution_norm_stop_partial": NONHARD["no_attribution_norm_stop_partial"],  # fallback :95-97
    "E12H2_no_attribution_norm_empty": NONHARD["no_attribution_norm_empty"],  # fallback :95-97
    "E21H2_stop_loss_label_norm_stop_partial": dict(sell_source="stop_loss", sources=("technical", "stop_loss"),
                                                   normalized_action="SELL_STOP_PARTIAL"),
    "E21H2_stop_loss_label_norm_empty": dict(sell_source="stop_loss", sources=("technical", "stop_loss"),
                                             normalized_action=""),
    "AC3s2_no_flag_exit_source_stop_loss": dict(sell_source="stop_loss", normalized_action="SELL_STOP_PARTIAL"),
}


class TestRev1CycleWithoutLongHardStopStaysRejected:
    @pytest.mark.parametrize("tid", TENANTS)
    @pytest.mark.parametrize("shape", list(SHAPES))
    def test_cycle_partial_rejected_no_full(self, shape, tid):
        i = list(SHAPES).index(shape)
        sym = f"R1C{i}{tid[:1].upper()}/USDT"
        an = _analysis(sym, action="SELL_STOP_PARTIAL", long_hard_stop=False, **SHAPES[shape])
        with _tenant(tid), _key_missing():
            _seed_t(sym, AMT)
            with _paper(), _logs_lvl() as lines:
                res = _run(_orch(), sym, HBAR_PX, an)
            _assert_blocked(res, sym, lines)

    @pytest.mark.parametrize("tid", TENANTS)
    def test_control_long_hard_stop_still_one_full(self, tid):
        sym = f"R1CC{tid[:1].upper()}/USDT"
        with _tenant(tid), _key_missing():
            _seed_t(sym, AMT)
            with _paper(), _logs_lvl() as lines:
                res = _run(_orch(), sym, HBAR_PX, _analysis(sym))
            assert res is not None and res.executed, getattr(res, "message", None)
            assert len(_fulls(sym)) == 1 and _held(sym) == pytest.approx(0.0, abs=1e-9)
        up = _upgrades(lines)
        assert len(up) == 1 and "reason=partial_sell_guard" in up[0] and "origin=long_hard_stop" in up[0], up


class TestRev1E27H2ExecuteOrderAndRiskDirect:
    @pytest.mark.parametrize("tid", TENANTS)
    def test_E27H2_inverted_execute_order_flagless_stop_loss_rejected(self, tid):
        from services.trading_service import TradingService

        sym = f"R1E27{tid[:1].upper()}/USDT"
        with _tenant(tid), _key_missing():
            _seed_t(sym, AMT)
            order = TradeOrder(type="SELL", symbol=sym, price=HBAR_PX, amount=AMT * 0.5, signal="SELL_STOP_PARTIAL",
                               source="auto", exit_source="stop_loss")
            with _paper(), _logs_lvl() as lines, patch("strategies.registry.resolve_strategy_params",
                                                       return_value=_params()):
                res = TradingService().execute_order(order, timeframe=TF, source="auto")
            _assert_blocked(res, sym, lines)

    @pytest.mark.parametrize("tid", TENANTS)
    @pytest.mark.parametrize("src", ["auto", "mcp", "dca_sniper_fund", "recovery"])
    def test_risk_direct_stop_loss_label_without_origin_rejected(self, src, tid):
        sym = f"R1RD{src[:3].upper()}{tid[:1].upper()}/USDT"
        with _tenant(tid), _key_missing():
            _seed_t(sym, AMT)
            order = TradeOrder(type="SELL", symbol=sym, price=HBAR_PX, amount=AMT * 0.5, signal="SELL_STOP_PARTIAL",
                               source=src, exit_source="stop_loss")
            with _logs_lvl() as lines, patch("strategies.registry.resolve_strategy_params", return_value=_params()):
                dec = _rm().evaluate(order, TF, source=src, locked_sell_checks=True)
        assert not dec.approved and dec.code == "partial_sell_guard", (dec.approved, dec.code, dec.message)
        assert getattr(dec, "order", None) is None or dec.order.signal != "SELL_STOP_FULL"
        assert not _upgrades(lines), lines
class TestRev1E18E22E23E25EscalationHelperNeedsOrigin:
    """Rev1 inversions at the _escalate_rejected_hard_partial level (Variant-A pins deleted under Rev1):
    E18H2[auto-*], E22 control_flagged_stop_loss_auto_escalates, E23H2 (all flag values), E25H2. An auto stop_loss
    partial built outside the orchestrator / WS path has no explicit origin -> None (rejection stands), any #723 flag
    value. This restores Viktor's original #723 E25 intent ("orders built elsewhere never escalate, fail closed").
    exit_ws stays escalated (control)."""

    @staticmethod
    def _call(sym, src, flag):
        order = TradeOrder(type="SELL", symbol=sym, price=HBAR_PX, amount=1.0, signal="SELL_STOP_PARTIAL",
                           source=src, exit_source="stop_loss")
        if flag is not None:
            order._long_hard_stop = flag
        return order, _rm()._escalate_rejected_hard_partial(order, TF, src, "x", code="sold_cap_unconfigured")

    @pytest.mark.parametrize("tid", TENANTS)
    @pytest.mark.parametrize("flag", [None, False, True, 1, "true"], ids=["absent", "false", "true", "int_one", "string"])
    def test_auto_stop_loss_without_origin_not_escalated(self, flag, tid):
        sym = f"R1H{str(flag)[:3].upper()}{tid[:1].upper()}/USDT"
        with _tenant(tid), _key_missing():
            _seed_t(sym, AMT)
            order, out = self._call(sym, "auto", flag)
        assert out is None and order.signal == "SELL_STOP_PARTIAL" and order.amount == 1.0
