"""AC1 (cycle-silence spec): a held lot with the hard stop hit reaches stop evaluation in the cycle even when GRID
is the selected strategy (stop evaluated in DecisionEngine._merge_sell). Driven through the REAL worker entry
services.eval_queue_runtime.process_eval_job (reason position_heartbeat, priority 30) -> SignalOrchestrator.process_coin
-> DecisionEngine.evaluate -> get_strategy -> GridStrategy.analyze / _merge_sell -> execute_if_needed ->
TradingService.execute_order -> RiskManager.evaluate. Edge mocks only (cs_helpers.paper).

Acceptance metric = a source=auto ORDER is CREATED and reaches risk evaluation (any status), not a fill.
Repo values (config.json @930792a3): stop_loss_pct 50 (:117), risk.partial_stop_ratio 0.67 (:623) -> stable partial
threshold 33.5 %, allocator default_grid_weight 0.4 / default_momentum_weight 0.6 (:1988-1989).
"""
from __future__ import annotations

import pytest

from cs_helpers import CFG, auto_orders, held, job, orchestrator, paper, seed_lot, spies, tenant

TENANTS = [pytest.param("default", id="default"), pytest.param("henry", id="henry")]
GRID = (0.7, 0.3)  # forces the REAL get_strategy (strategies/registry.py:541-544) to pick GridStrategy
LOT = 2000.0  # 2000 * 0.62 = 1240 USDT, half 620 > repo floors 500/300: the size guard is not what is measured


def _run(sym, tid, px, grid=GRID, **lot_extra):
    from services.eval_queue_runtime import process_eval_job

    with tenant(tid):
        seed_lot(sym, LOT, **lot_extra)
        with paper({sym: px}, grid_weight=grid), spies() as rec:
            out = process_eval_job(orchestrator(), job(sym, tid=tid))
        rows = auto_orders(sym)
        return out, rec, rows, held(sym)


def test_repo_values():
    assert float(CFG["stop_loss_pct"]) == 50.0
    assert float(CFG["risk"]["partial_stop_ratio"]) == 0.67
    sa = CFG["strategy_allocator"]
    assert (sa["default_grid_weight"], sa["default_momentum_weight"]) == (0.4, 0.6)


class TestAC1GridWinsStopStillEvaluated:
    @pytest.mark.parametrize("tid", TENANTS)
    def test_grid_selected_partial_band_creates_auto_stop_partial(self, tid):
        sym = f"CS1P{tid[:1].upper()}/USDT"
        out, rec, rows, _ = _run(sym, tid, 0.62)  # -38 % > 33.5 % partial, < 50 % full
        assert rec["grid"] and not rec["analyze"], rec  # GridStrategy really was the selected class
        orders = [o for o in rec["orders"] if o["symbol"] == sym]
        assert len(orders) == 1, rec
        o = orders[0]
        assert (o["signal"], o["source"], o["exit_source"], o["tenant"]) == ("SELL_STOP_PARTIAL", "auto", "stop_loss", tid)
        assert o["amount"] == pytest.approx(LOT * 0.5, rel=1e-9)
        assert [r for r in rec["risk"] if r["symbol"] == sym and r["locked"]], rec["risk"]  # reached risk evaluation
        assert len(rows) == 1 and rows[0]["signal"] == "SELL_STOP_PARTIAL", rows

    @pytest.mark.parametrize("tid", TENANTS)
    def test_grid_selected_full_band_creates_auto_stop_full(self, tid):
        sym = f"CS1F{tid[:1].upper()}/USDT"
        out, rec, rows, _ = _run(sym, tid, 0.45)  # -55 % > 50 %
        assert rec["grid"] and not rec["analyze"], rec
        orders = [o for o in rec["orders"] if o["symbol"] == sym]
        assert len(orders) == 1 and orders[0]["signal"] == "SELL_STOP_FULL", rec
        assert orders[0]["source"] == "auto" and orders[0]["exit_source"] == "stop_loss"
        assert orders[0]["amount"] == pytest.approx(LOT, rel=1e-9)
        assert len(rows) == 1, rows

    @pytest.mark.parametrize("tid", TENANTS)
    def test_order_created_and_reaches_risk_even_if_risk_rejects(self, tid):
        """Second-steering (b): the metric is ORDER CREATED. A real guard (partial_sell_guard sold cap, not touched
        by H2 or by this fix) rejects it; the source=auto row must still exist with status rejected."""
        sym = f"CS1R{tid[:1].upper()}/USDT"
        out, rec, rows, h = _run(sym, tid, 0.62, sold_percent=0.995)
        assert [r for r in rec["risk"] if r["symbol"] == sym and not r["approved"]], rec["risk"]
        assert len(rows) == 1, rows
        assert rows[0]["status"] == "rejected" and rows[0]["signal"] == "SELL_STOP_PARTIAL"
        assert rows[0]["exit_source"] == "stop_loss" and rows[0]["source"] == "auto"
        assert h == pytest.approx(LOT)

    def test_grid_selected_below_threshold_no_order(self):
        sym = "CS1N/USDT"
        out, rec, rows, _ = _run(sym, "default", 0.80)  # -20 % < 33.5 %: guards the threshold
        assert rec["grid"] and rec["orders"] == [] and rows == []

    @pytest.mark.parametrize("tid", TENANTS)
    def test_control_technical_path_still_one_stop(self, tid):
        """Committed weights 0.4/0.6 -> TechnicalRSIStrategy. Green on base: pins that the merge-site call does not
        add a SECOND stop candidate/order when the technical strategy already produced one."""
        sym = f"CS1T{tid[:1].upper()}/USDT"
        out, rec, rows, _ = _run(sym, tid, 0.62, grid=None)
        assert rec["analyze"] and not rec["grid"], rec
        orders = [o for o in rec["orders"] if o["symbol"] == sym]
        assert len(orders) == 1 and orders[0]["signal"] == "SELL_STOP_PARTIAL" and orders[0]["source"] == "auto", rec
        assert len(rows) == 1
