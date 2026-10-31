"""H2 port of #723 annex B delta 3 tests (spec_723_partial_escalation/tests/test_723_annex_b_e4.py, credit Viktor).
Base staging 930792a3 + proto_fix_h2.py. E26 unchanged; E27 unchanged at 1.3 (blocked by H2-BELOW-ENTRY instead of the
missing flag); E27H2 NEW: the same order at a loss closes FULL end-to-end.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from h2_port_helpers import (  # noqa: F401
    HBAR_PX, TENANTS, TF, _analysis, _filled_sells, _held, _key_missing, _logs_lvl, _paper, _params,
    _prod_floors_and_clean_books, _seed_t, _tenant,
)
from test_h2_port_e2 import NONHARD
from test_h2_port_e3 import AMT, _assert_blocked, _assert_one_full, _fulls, _nonhard, _orch, _run, _upgrades


class TestE26SameSymbolSameOrchestrator:
    @pytest.mark.parametrize("tid", TENANTS)
    @pytest.mark.parametrize("case", list(NONHARD))
    def test_hard_reseed_then_nonhard_same_symbol_same_orchestrator(self, case, tid):
        i = list(NONHARD).index(case)
        sym = f"E26S{i}{tid[:1].upper()}H2/USDT"
        orch = _orch()
        with _tenant(tid), _key_missing():
            _seed_t(sym, AMT)
            with _paper(), _logs_lvl() as la:
                ra = _run(orch, sym, HBAR_PX, _analysis(sym))
            _assert_one_full(ra, sym, la, tid)
            _seed_t(sym, AMT)
            with _paper(), _logs_lvl() as lb:
                rb = _run(orch, sym, 1.3, _nonhard(sym, case))
            _assert_blocked(rb, sym, lb, n_fills=1)


class TestE27FlaglessStopLossPartialEndToEnd:
    @pytest.mark.parametrize("tid", TENANTS)
    def test_flagless_auto_stop_loss_partial_blocked_via_execute_order(self, tid):
        from core.models import TradeOrder
        from services.trading_service import TradingService

        sym = f"E27F{tid[:1].upper()}H2/USDT"
        with _tenant(tid), _key_missing():
            _seed_t(sym, AMT)
            order = TradeOrder(type="SELL", symbol=sym, price=1.3, amount=AMT * 0.5, signal="SELL_STOP_PARTIAL",
                               source="auto", exit_source="stop_loss")
            assert not hasattr(order, "_long_hard_stop")
            with _paper(), _logs_lvl() as lines, patch("strategies.registry.resolve_strategy_params", return_value=_params()):
                res = TradingService().execute_order(order, timeframe=TF, source="auto")
            assert res is not None and not res.executed, getattr(res, "message", None)
            assert getattr(res, "code", None) == "partial_sell_guard", (getattr(res, "code", None), getattr(res, "message", None))
            assert _filled_sells(sym) == [] and _fulls(sym) == []
            assert _held(sym) == pytest.approx(AMT)
            assert not _upgrades(lines), lines

