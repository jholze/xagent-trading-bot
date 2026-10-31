"""H2 port of #723 annex B delta 2 tests (spec_723_partial_escalation/tests/test_723_annex_b_e3.py, credit Lena/Viktor).
Base staging 930792a3 + proto_fix_h2.py. Key risk.block_partial_sell_if_sold_percent_above missing throughout.
Adapted to the H2 rule (is_hard_stop_order, no cycle flag):
  E19/E20/E22/E24/E12u  unchanged (non-hard shapes at 1.3 stay blocked; P723-HARD barrier = H2-HARD).
  E21      unchanged at 1.3 (blocked by H2-BELOW-ENTRY); E21H2 NEW: the same label at a loss -> one FULL.
  E23/E25  INVERTED: the flag is not read; a stop_loss/auto hard partial escalates.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from h2_port_helpers import (  # noqa: F401  (autouse fixture imported by name)
    CAP_KEY, HBAR_PX, TENANTS, TF, _analysis, _errors, _filled_sells, _held, _key_missing, _logs_lvl, _paper, _params,
    _prod_floors_and_clean_books, _repo_config, _rm, _seed_t, _tenant,
)
from test_h2_port_e2 import NONHARD


AMT = 700.0 / HBAR_PX
CASES = list(NONHARD)


def _orch():
    from services.signal_orchestrator import SignalOrchestrator

    return SignalOrchestrator(notify_callback=lambda *a, **k: None)


def _run(orch, sym, price, an):
    with patch("strategies.registry.resolve_strategy_params", return_value=_params()):
        return orch.execute_if_needed(an, {"symbol": sym, "timeframe": TF}, price)


def _nonhard(sym, case):
    return _analysis(sym, action="SELL_STOP_PARTIAL", long_hard_stop=False, **NONHARD[case])


def _fulls(sym):
    return [o for o in _filled_sells(sym) if o.get("signal") == "SELL_STOP_FULL"]


def _upgrades(lines):
    return [m for _, m in lines if "partial_stop_upgraded_to_full" in m]


def _assert_blocked(res, sym, lines, n_fills=0):
    assert res is not None and not res.executed and res.code == "partial_sell_guard", (
        getattr(res, "code", None), getattr(res, "message", None))
    assert len(_filled_sells(sym)) == n_fills and len(_fulls(sym)) == n_fills
    assert _held(sym) == pytest.approx(AMT)
    assert not _upgrades(lines), lines


def _assert_one_full(res, sym, lines, tid):
    assert res is not None and res.executed, getattr(res, "message", None)
    assert len(_fulls(sym)) == 1 and len(_filled_sells(sym)) == 1
    assert _held(sym) == pytest.approx(0.0, abs=1e-9)
    assert _errors(lines, "partial_stop_upgraded_to_full", "block=sold_cap_unconfigured", f"tenant={tid}"), lines


class TestE19LongLivedOrchestrator:
    @pytest.mark.parametrize("tid", TENANTS)
    @pytest.mark.parametrize("case", CASES)
    def test_hard_then_nonhard_same_orchestrator(self, case, tid):
        i = CASES.index(case)
        a, b = f"E19HA{i}{tid[:1].upper()}H2/USDT", f"E19NB{i}{tid[:1].upper()}H2/USDT"
        orch = _orch()
        with _tenant(tid), _key_missing():
            _seed_t(a, AMT)
            _seed_t(b, AMT)
            with _paper():
                with _logs_lvl() as la:
                    ra = _run(orch, a, HBAR_PX, _analysis(a))
                _assert_one_full(ra, a, la, tid)
                with _logs_lvl() as lb:
                    rb = _run(orch, b, 1.3, _nonhard(b, case))
            _assert_blocked(rb, b, lb)

    @pytest.mark.parametrize("tid", TENANTS)
    @pytest.mark.parametrize("case", CASES)
    def test_nonhard_then_hard_same_orchestrator(self, case, tid):
        i = CASES.index(case)
        a, b = f"E19NA{i}{tid[:1].upper()}H2/USDT", f"E19HB{i}{tid[:1].upper()}H2/USDT"
        orch = _orch()
        with _tenant(tid), _key_missing():
            _seed_t(a, AMT)
            _seed_t(b, AMT)
            with _paper():
                with _logs_lvl() as la:
                    ra = _run(orch, a, 1.3, _nonhard(a, case))
                _assert_blocked(ra, a, la)
                with _logs_lvl() as lb:
                    rb = _run(orch, b, HBAR_PX, _analysis(b))
            _assert_one_full(rb, b, lb, tid)


class TestE20SameSymbolAfterReseed:
    @pytest.mark.parametrize("tid", TENANTS)
    def test_hard_reseed_then_nonhard_same_symbol(self, tid):
        sym = f"E20SAME{tid[:1].upper()}H2/USDT"
        with _tenant(tid), _key_missing():
            _seed_t(sym, AMT)
            with _paper(), _logs_lvl() as la:
                ra = _run(_orch(), sym, HBAR_PX, _analysis(sym))
            _assert_one_full(ra, sym, la, tid)
            _seed_t(sym, AMT)
            with _paper(), _logs_lvl() as lb:
                rb = _run(_orch(), sym, 1.3, _nonhard(sym, "no_attribution_norm_stop_partial"))
            _assert_blocked(rb, sym, lb, n_fills=1)


class TestE21StopLossSellSourceWithoutFlag:
    """Unchanged at 1.3 (above entry): blocked by H2-BELOW-ENTRY (no longer by a missing flag)."""

    @pytest.mark.parametrize("tid", TENANTS)
    @pytest.mark.parametrize("norm", ["SELL_STOP_PARTIAL", ""], ids=["norm_stop_partial", "norm_empty"])
    def test_stop_loss_label_without_hard_flag_blocked(self, norm, tid):
        sym = f"E21SL{len(norm)}{tid[:1].upper()}H2/USDT"
        an = _analysis(sym, action="SELL_STOP_PARTIAL", long_hard_stop=False, sell_source="stop_loss",
                       sources=("technical", "stop_loss"), normalized_action=norm)
        with _tenant(tid), _key_missing():
            _seed_t(sym, AMT)
            with _paper(), _logs_lvl() as lines:
                res = _run(_orch(), sym, 1.3, an)
            _assert_blocked(res, sym, lines)

def _direct(sym, src, exit_source, flag):
    from core.models import TradeOrder

    order = TradeOrder(type="SELL", symbol=sym, price=HBAR_PX, amount=1.0, signal="SELL_STOP_PARTIAL",
                       source=src, exit_source=exit_source)
    if flag is not None:
        order._long_hard_stop = flag
    return order, _rm()._escalate_rejected_hard_partial(order, TF, src, "x", code="sold_cap_unconfigured")


class TestE22P723HardIsOwnBarrier:
    @pytest.mark.parametrize("tid", TENANTS)
    @pytest.mark.parametrize("src,ex", [("auto", "take_profit"), ("auto", "trailing_stop"), ("auto", None),
                                        ("manual", "stop_loss")],
                             ids=["take_profit_exit", "trailing_exit", "no_exit_source", "manual_source"])
    def test_flagged_order_failing_p723_hard_not_escalated(self, src, ex, tid):
        sym = f"E22{src[:2].upper()}{str(ex)[:2].upper()}{tid[:1].upper()}H2/USDT"
        with _tenant(tid), _key_missing():
            _seed_t(sym, AMT)
            order, out = _direct(sym, src, ex, True)
            assert out is None and order.signal == "SELL_STOP_PARTIAL" and order.amount == 1.0
class TestE24ReportAcceptsCapOne:
    @pytest.mark.parametrize("tid", TENANTS)
    @pytest.mark.parametrize("cap", [1.0, 1], ids=["float", "int"])
    def test_report_and_assert_accept_cap_one(self, cap, tid):
        from core.config_guardrails import assert_required_keys_present, report_missing_required_keys

        cfg = _repo_config()
        cfg["risk"][CAP_KEY] = cap
        with _logs_lvl() as lines:
            assert report_missing_required_keys(cfg, tenant=tid) == []
        assert not _errors(lines, "config_required_key_missing"), lines
        assert_required_keys_present(cfg)


class TestE12uNonHardUniqueSymbols:
    @pytest.mark.parametrize("tid", TENANTS)
    @pytest.mark.parametrize("case", CASES)
    def test_nonhard_blocked_unique_symbol(self, case, tid):
        sym = f"E12U{CASES.index(case)}{tid[:1].upper()}H2/USDT"
        with _tenant(tid), _key_missing():
            _seed_t(sym, AMT)
            with _paper(), _logs_lvl() as lines:
                res = _run(_orch(), sym, 1.3, _nonhard(sym, case))
            _assert_blocked(res, sym, lines)
            assert "not configured" in str(getattr(res, "message", ""))

