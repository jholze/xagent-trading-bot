"""AC6 (cycle-silence spec): tenant isolation of the merge-site stop and the queue fixes. The SAME symbol is held
by both tenants with different lots; each tenant's cycle (REAL process_eval_job, grid selected) decides only on its
own lot and writes only its own ledger; the queue keeps one heartbeat member per tenant.
"""
from __future__ import annotations

import pytest

from cs_helpers import QCFG, TF, fake_queue, held, job, orchestrator, orders_for, paper, seed_lot, spies, tenant

GRID = (0.7, 0.3)
SYM = "CS6X/USDT"
PX = 0.62


@pytest.mark.parametrize("hit,miss", [("default", "henry"), ("henry", "default")])
def test_same_symbol_only_hit_tenant_stops(hit, miss):
    from services.eval_queue_runtime import process_eval_job

    sym = f"CS6{hit[:1].upper()}/USDT"
    with tenant(hit):
        seed_lot(sym, 2000.0, entry=1.0)  # -38 %: stop due
    with tenant(miss):
        seed_lot(sym, 2000.0, entry=0.70)  # -11 %: no stop
    for tid in (hit, miss):
        with tenant(tid):
            with paper({sym: PX}, grid_weight=GRID), spies() as rec:
                process_eval_job(orchestrator(), job(sym, tid=tid))
            mine = [o for o in rec["orders"] if o["symbol"] == sym]
            if tid == hit:
                assert len(mine) == 1 and mine[0]["tenant"] == hit and mine[0]["exit_source"] == "stop_loss", rec
            else:
                assert mine == [], rec
    with tenant(miss):
        assert orders_for(sym) == [] and held(sym) == pytest.approx(2000.0)
    with tenant(hit):
        assert len([o for o in orders_for(sym) if o.get("exit_source") == "stop_loss"]) == 1  # label: AC1


def test_queue_heartbeats_per_tenant_not_coalesced_across_tenants(monkeypatch):
    """Prod runs multi-tenant (config.json:2040-2041 multi_tenant.enabled true) -> member key tenant|symbol|tf
    (bus/eval_queue.py:108-117). default's entry job neither replaces henry's heartbeat nor is turned into a heartbeat
    (no cross-tenant coalescing). Same-tenant coalescing is AC2."""
    from bus.eval_queue import enqueue_eval, pop_eval_batch

    monkeypatch.setenv("MULTI_TENANT_ENABLED", "1")
    with fake_queue():
        assert enqueue_eval(SYM, TF, reason="position_heartbeat", priority=30, config_raw=QCFG, tenant_id="henry")
        assert enqueue_eval(SYM, TF, reason="entry_15m_vol_spike", priority=15, config_raw=QCFG, tenant_id="default")
        jobs = pop_eval_batch(10, config_raw=QCFG)
    got = sorted((j.tenant_id, j.reason) for j in jobs if j.symbol == SYM)
    assert got == [("default", "entry_15m_vol_spike"), ("henry", "position_heartbeat")], got
