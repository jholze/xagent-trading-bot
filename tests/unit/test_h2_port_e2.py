"""H2 port of #723 annex B delta 1 tests (spec_723_partial_escalation/tests/test_723_annex_b_e2.py, credit Lena/Viktor/Kay).
Base staging 930792a3 + proto_fix_h2.py. Adapted to the H2 rule (is_hard_stop_order, no cycle flag, WS included):
  E12      unchanged (price 1.3 > entry: the no_attribution shapes are blocked by H2-BELOW-ENTRY, the rest are non-hard).
  E12H2    NEW: the no_attribution shapes at a loss are hard stops -> one FULL (inverts #723 F1 for them).
  E13-E17  unchanged.
  E18      INVERTED: exit_ws and auto both escalate, flag irrelevant.
NONHARD kept verbatim (imported by the e3/e4 ports).
"""
from __future__ import annotations

import pytest

from h2_port_helpers import (  # noqa: F401  (autouse fixture imported by name)
    BAD_CAPS, CAP_KEY, ENTRY, FULL_ZONE_PX, HBAR_AMOUNT, HBAR_PEAK, HBAR_PX, MAX_SOLD, TENANTS, TF, _POP, _analysis,
    _assert_one_full, _cycle, _errors, _filled_sells, _fire, _full_analysis, _guard_call, _held, _hub, _key_missing,
    _logs_lvl, _orders, _paper, _params, _prod_floors_and_clean_books, _repo_config, _rm, _row, _seed_t,
    _strategy_override, _tenant,
)


NONHARD = {
    "tp_norm_empty": dict(sell_source="take_profit", sources=("technical",), normalized_action=""),
    "tp_norm_stop_partial": dict(sell_source="take_profit", sources=("technical",),
                                 normalized_action="SELL_STOP_PARTIAL"),
    "trail_norm_empty": dict(sell_source="trailing_stop", sources=("technical",), normalized_action=""),
    "no_attribution_norm_stop_partial": dict(sell_source="", sources=(), normalized_action="SELL_STOP_PARTIAL"),
    "no_attribution_norm_empty": dict(sell_source="", sources=(), normalized_action=""),
}


def _fulls(sym):
    return [o for o in _filled_sells(sym) if o.get("signal") == "SELL_STOP_FULL"]


class TestE12NonHardPartialKeyMissingBlockOnly:
    """Unchanged at price 1.3 (above entry). The two no_attribution_* shapes resolve to exit_source stop_loss
    (exit_attribution.py:95-97) and ARE hard stops by is_hard_stop_order; at 1.3 they stay blocked by H2-BELOW-ENTRY."""

    @pytest.mark.parametrize("tid", TENANTS)
    @pytest.mark.parametrize("case", list(NONHARD))
    def test_nonhard_stop_partial_blocked_no_fill_no_upgrade(self, case, tid):
        sym = f"E12{case[:6].upper().replace('_', '')}{tid[:1].upper()}H2/USDT"
        amount = 700.0 / HBAR_PX
        an = _analysis(sym, action="SELL_STOP_PARTIAL", long_hard_stop=False, **NONHARD[case])
        with _tenant(tid), _key_missing():
            _seed_t(sym, amount)
            with _paper(), _logs_lvl() as lines:
                res = _cycle(sym, 1.3, analysis=an)
            assert res is not None and not res.executed and res.code == "partial_sell_guard", (
                getattr(res, "code", None), getattr(res, "message", None))
            assert "not configured" in str(getattr(res, "message", "")), getattr(res, "message", None)
            assert _filled_sells(sym) == [] and _fulls(sym) == []
            assert _held(sym) == pytest.approx(amount)
            assert not [m for _, m in lines if "partial_stop_upgraded_to_full" in m], lines

    @pytest.mark.parametrize("tid", TENANTS)
    def test_hard_control_still_exactly_one_full(self, tid):
        sym = f"E12HARD{tid[:1].upper()}H2/USDT"
        amount = 700.0 / HBAR_PX
        with _tenant(tid), _key_missing():
            _seed_t(sym, amount)
            with _paper(), _logs_lvl() as lines:
                res = _cycle(sym, HBAR_PX)
            assert res is not None and res.executed, getattr(res, "message", None)
            assert len(_fulls(sym)) == 1 and len(_filled_sells(sym)) == 1
            assert _held(sym) == pytest.approx(0.0, abs=1e-9)
            assert _errors(lines, "partial_stop_upgraded_to_full", "block=sold_cap_unconfigured", f"tenant={tid}")


class TestE13ShortSideNotApplicable:
    @pytest.mark.parametrize("tid", TENANTS)
    @pytest.mark.parametrize("signal", ["COVER_FULL", "COVER_STOP_PARTIAL"])
    def test_cover_orders_are_never_escalated(self, signal, tid):
        from core.models import TradeOrder
        from risk.risk_manager import is_hard_stop_order

        sym = f"E13{signal[:5]}{tid[:1].upper()}H2/USDT"
        with _tenant(tid), _key_missing():
            _seed_t(sym, 700.0 / HBAR_PX)
            order = TradeOrder(type="BUY", symbol=sym, price=HBAR_PX, amount=1.0, signal=signal,
                               source="auto", exit_source="stop_loss")
            assert is_hard_stop_order(order, "auto") is False
            assert _rm()._escalate_rejected_hard_partial(order, TF, "auto", "x", code="sold_cap_unconfigured") is None
            assert order.signal == signal and order.amount == 1.0


def _escalate_once(tid, sym):
    amount = 250.0 / HBAR_PX
    with _tenant(tid), _key_missing():
        _seed_t(sym, amount, peak_amount=amount * 2, sold_percent=0.5)
        with _paper(), _logs_lvl() as lines:
            res = _cycle(sym, HBAR_PX)
        assert res is not None and res.executed, getattr(res, "message", None)
    return lines


class TestE14UpgradeErrorLineEveryEscalation:
    def test_same_symbol_default_then_henry_each_logs(self):
        sym = "E14SAMEH2/USDT"
        l1 = _escalate_once("default", sym)
        l2 = _escalate_once("henry", sym)
        assert _errors(l1, "partial_stop_upgraded_to_full", "block=sold_cap_unconfigured", "tenant=default"), l1
        assert _errors(l2, "partial_stop_upgraded_to_full", "block=sold_cap_unconfigured", "tenant=henry"), l2

    @pytest.mark.parametrize("tid", TENANTS)
    def test_second_escalation_same_tenant_logs_again(self, tid):
        sym = f"E14RE{tid[:1].upper()}H2/USDT"
        l1 = _escalate_once(tid, sym)
        l2 = _escalate_once(tid, sym)
        assert _errors(l1, "partial_stop_upgraded_to_full", "block=sold_cap_unconfigured"), l1
        assert _errors(l2, "partial_stop_upgraded_to_full", "block=sold_cap_unconfigured"), l2


class TestE15StartupReportFlagsInvalidValues:
    @pytest.mark.parametrize("tid", TENANTS)
    @pytest.mark.parametrize("bad", [1.5, 0, -0.5, "0.99", True, None, _POP],
                             ids=["above_one", "zero", "negative", "string", "bool", "none", "missing"])
    def test_report_flags_invalid(self, tid, bad):
        from core.config_guardrails import report_missing_required_keys

        cfg = _repo_config()
        if bad is _POP:
            cfg["risk"].pop(CAP_KEY, None)
        else:
            cfg["risk"][CAP_KEY] = bad
        with _logs_lvl() as lines:
            got = report_missing_required_keys(cfg, tenant=tid)
        assert got == ["risk." + CAP_KEY]
        assert _errors(lines, "config_required_key_missing", CAP_KEY, f"tenant={tid}"), lines


class TestE16SizeCodeEscalationLogsInfo:
    @pytest.mark.parametrize("tid", TENANTS)
    def test_min_position_escalation_is_info_with_key_set(self, tid):
        sym = f"E16MINPOS{tid[:1].upper()}H2/USDT"
        amount = 250.0 / HBAR_PX
        with _tenant(tid):
            _seed_t(sym, amount, peak_amount=amount * 2, sold_percent=0.5)
            with _paper(), _logs_lvl() as lines:
                res = _cycle(sym, HBAR_PX)
            assert res is not None and res.executed, getattr(res, "message", None)
        up = [(lvl, m) for lvl, m in lines if "partial_stop_upgraded_to_full" in m]
        assert up and all("block=min_position" in m for _, m in up), up
        assert all(lvl == "INFO" for lvl, _ in up), up


class TestE17CapExactlyOneIsValidAtRuntime:
    @pytest.mark.parametrize("tid", TENANTS)
    @pytest.mark.parametrize("cap", [1.0, 1], ids=["float", "int"])
    def test_cap_one_is_configured(self, cap, tid):

        sym = f"E17ONE{tid[:1].upper()}{type(cap).__name__[:1]}H2/USDT"
        with _tenant(tid), _key_missing(cap), _logs_lvl() as lines:
            assert _rm()._partial_sell_limits(sym, TF)["max_sold_percent"] == pytest.approx(1.0)
        assert not _errors(lines, "partial_sell_cap_missing"), lines

    @pytest.mark.parametrize("tid", TENANTS)
    def test_cap_one_big_partial_executes_as_partial(self, tid):
        """cap 1.0 configured: a 1400 USDT hard partial is NOT blocked (no unconfigured path) -> partial fill, no FULL."""
        sym = f"E17BIG{tid[:1].upper()}H2/USDT"
        amount = 1400.0 / HBAR_PX
        with _tenant(tid), _key_missing(1.0):
            _seed_t(sym, amount)
            with _paper(), _logs_lvl() as lines:
                res = _cycle(sym, HBAR_PX)
            assert res is not None and res.executed, getattr(res, "message", None)
            assert _fulls(sym) == [] and [o.get("signal") for o in _filled_sells(sym)] == ["SELL_STOP_PARTIAL"]
            assert 0 < _held(sym) < amount
        assert not [m for _, m in lines if "partial_stop_upgraded_to_full" in m], lines
