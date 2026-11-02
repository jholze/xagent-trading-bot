"""AC5 (cycle-silence spec, JOINT with H2): a cycle hard-stop partial below the minimum size, produced by the cycle
for a GRID-selected held lot and run through the REAL process_eval_job, ends in exactly ONE SELL_STOP_FULL fill.
Needs both: the cycle fix (the stop is evaluated at all when grid wins, CS-F1) and H2 (risk escalates a size-rejected
hard partial to exactly one FULL). Double-fill guard (Lena 3): cycle stop + WS hub on the same symbol -> one fill,
in both orders. The H2 AC5 file test_ac5_ws_ticks_plus_cycle_one_fill.py is shipped byte-identical in this bundle.

Repo floors (config.json:616-617): min_sell_notional_usdt 300, min_position_usdt_for_partial_sell 500.
Fixture lot 400 units @ entry 1.0, price 0.62 -> 248 USDT (< 300 < 500), half = 124 USDT; -38 % > 33.5 % partial.
"""
from __future__ import annotations

import json

import pytest

from cs_helpers import CFG, TF, held, job, orchestrator, orders_for, paper, seed_lot, spies, tenant

TENANTS = [pytest.param("default", id="default"), pytest.param("henry", id="henry")]
GRID = (0.7, 0.3)
AMOUNT, PX = 400.0, 0.62
RISK = CFG["risk"]


@pytest.fixture(autouse=True)
def _repo_floors():
    import data_manager
    import services.exit_realtime.execute as ex

    risk = data_manager.get_config().setdefault("risk", {})
    keys = ("min_sell_notional_usdt", "min_position_usdt_for_partial_sell", "block_partial_sell_if_sold_percent_above")
    saved = {k: risk.get(k) for k in keys}
    for k in keys:
        risk[k] = RISK[k]
    ex._inflight.clear(); ex._last_exit_at.clear()
    yield
    ex._inflight.clear(); ex._last_exit_at.clear()
    for k, v in saved.items():
        if v is None:
            risk.pop(k, None)
        else:
            risk[k] = v


def _fills(sym):
    return [o for o in orders_for(sym) if o.get("status") == "filled"
            and str(o.get("type") or o.get("side") or "").upper() == "SELL"]


def _desc(sym):
    return [(o.get("signal"), o.get("source"), o.get("status"), o.get("amount")) for o in orders_for(sym)]


def _cycle(sym, tid):
    from services.eval_queue_runtime import process_eval_job

    return process_eval_job(orchestrator(), job(sym, tid=tid))


def _ws_ticks(sym, lot, tid, n=10):
    import services.exit_realtime.execute as ex
    from services.exit_realtime.hub import ExitRealtimeHub
    from strategies.registry import resolve_strategy_params

    hub = ExitRealtimeHub({"exit_realtime": {"enabled": True, "mode": "live", "sources": ["stop_loss"],
                                             "live_cooldown_sec": 0.0, "default_atr_pct": 3.0}})
    params = dict(resolve_strategy_params({"symbol": sym, "timeframe": TF}, has_position=True) or {})
    params.setdefault("stop_loss_pct", float(CFG["stop_loss_pct"]))
    for _ in range(n):
        row = {"symbol": sym, "timeframe": TF, "position": dict(lot), "average_entry": 1.0, "recent_high": 1.0,
               "strategy_params": params, "atr_pct": 3.0}
        if tid != "default":
            row["tenant_id"] = tid
        hub.update_book([row])
        hub.handle_ws_message(json.dumps({"time": 1, "channel": "spot.tickers", "event": "update", "result": {
            "currency_pair": sym.replace("/", "_"), "last": str(PX), "highest_bid": str(PX),
            "change_percentage": "-12.5", "quote_volume": "1000"}}))
        ex._inflight.clear(); ex._last_exit_at.clear()


class TestAC5Joint:
    def test_fixture_below_floors(self):
        assert AMOUNT * PX < float(RISK["min_sell_notional_usdt"]) <= float(RISK["min_position_usdt_for_partial_sell"])

    @pytest.mark.parametrize("tid", TENANTS)
    def test_grid_cycle_small_hard_partial_one_full(self, tid):
        sym = f"CS5C{tid[:1].upper()}/USDT"
        with tenant(tid):
            seed_lot(sym, AMOUNT)
            with paper({sym: PX}, grid_weight=GRID), spies() as rec:
                _cycle(sym, tid)
            assert rec["grid"], rec
            cyc = [o for o in rec["orders"] if o["symbol"] == sym and o["source"] != "exit_ws"]
            assert cyc and cyc[0]["exit_source"] == "stop_loss", rec  # the cycle produced the stop order (label: AC1)
            fills = _fills(sym)
            assert len(fills) == 1, _desc(sym)
            assert fills[0].get("signal") == "SELL_STOP_FULL", _desc(sym)
            assert held(sym) == pytest.approx(0.0, abs=1e-12)

    @pytest.mark.parametrize("tid", TENANTS)
    def test_grid_cycle_then_ten_ws_ticks_one_fill(self, tid):
        sym = f"CS5D{tid[:1].upper()}/USDT"
        with tenant(tid):
            lot = seed_lot(sym, AMOUNT)
            with paper({sym: PX}, grid_weight=GRID):
                _cycle(sym, tid)
                _ws_ticks(sym, lot, tid)
            fills = _fills(sym)
            assert len(fills) == 1 and fills[0].get("signal") == "SELL_STOP_FULL", _desc(sym)
            assert held(sym) == pytest.approx(0.0, abs=1e-12)

    @pytest.mark.parametrize("tid", TENANTS)
    def test_ten_ws_ticks_then_grid_cycle_one_fill(self, tid):
        """Counted at the execute layer (TradingService.execute_order results), not the ledger: in the test env
        (demo mode) a no-position process_coin for tenant default empties the demo orders ledger on base 930792a3
        too (observed, unrelated to this fix; spec section 9)."""
        sym = f"CS5W{tid[:1].upper()}/USDT"
        with tenant(tid):
            lot = seed_lot(sym, AMOUNT)
            with paper({sym: PX}, grid_weight=GRID), spies() as rec:
                _ws_ticks(sym, lot, tid)
                _cycle(sym, tid)
            executed = [o for o in rec["orders"] if o["symbol"] == sym and o["executed"]]
            assert len(executed) == 1 and executed[0]["signal"] == "SELL_STOP_FULL", rec["orders"]
            assert executed[0]["amount"] == pytest.approx(AMOUNT, rel=1e-9)
            assert held(sym) == pytest.approx(0.0, abs=1e-12)
