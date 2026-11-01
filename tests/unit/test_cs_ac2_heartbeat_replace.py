"""AC2 (cycle-silence spec): an entry_15m / 15m_watch enqueue for the same tenant+symbol+timeframe must not
replace a PENDING position_heartbeat. Base defect: bus/eval_queue.py:170-194 ZADD+HSET overwrite the member's
reason; process_eval_job then takes the entry branch (services/eval_queue_runtime.py:202-204) and
_process_entry_15m_job returns HOLD for an open lot before analyze (:118-121).

Real enqueue_eval / pop_eval_batch on an in-memory Redis with real ZSET semantics (cs_helpers.FakeRedis), then the
REAL process_eval_job. Also: a NON-stop strategy exit for a held lot (rsi_sell) reaches analyze the same way
(heartbeat loss removes all cycle exits, not only the stop).
"""
from __future__ import annotations

import pytest

from cs_helpers import QCFG, auto_orders, fake_queue, held, orchestrator, paper, seed_lot, spies, tenant, TF

TENANTS = [pytest.param("default", id="default"), pytest.param("henry", id="henry")]
ENTRY_REASONS = [("entry_15m_vol_spike", 15), ("15m_watch", 15)]
PROFIT_IND = {"rsi": 85.0, "lower_bb": 1.0, "middle_bb": 1.2, "upper_bb": 1.35, "vol_multiplier": 1.0,
              "atr": 0.03, "atr_pct": 3.0}


def _enqueue_heartbeat_then_entry(sym, tid, entry_reason, entry_pri):
    from bus.eval_queue import PRIORITY_POSITION_HEARTBEAT, enqueue_eval, pop_eval_batch

    assert enqueue_eval(sym, TF, reason="position_heartbeat", priority=PRIORITY_POSITION_HEARTBEAT,
                        config_raw=QCFG, tenant_id=tid)
    enqueue_eval(sym, TF, reason=entry_reason, priority=entry_pri, config_raw=QCFG, tenant_id=tid)
    jobs = pop_eval_batch(10, config_raw=QCFG)
    return [j for j in jobs if j.symbol == sym]


class TestAC2EntryDoesNotReplaceHeartbeat:
    @pytest.mark.parametrize("tid", TENANTS)
    @pytest.mark.parametrize("entry_reason,entry_pri", ENTRY_REASONS)
    def test_held_lot_stop_still_analyzed(self, tid, entry_reason, entry_pri):
        from services.eval_queue_runtime import process_eval_job

        sym = f"CS2S{tid[:1].upper()}{entry_reason[:2].upper()}/USDT"
        with tenant(tid):
            seed_lot(sym, 2000.0)
            with fake_queue():
                jobs = _enqueue_heartbeat_then_entry(sym, tid, entry_reason, entry_pri)
            assert len(jobs) == 1, jobs  # one member per tenant|symbol|tf (bus/eval_queue.py:108-117)
            assert jobs[0].reason == "position_heartbeat", jobs[0]
            with paper({sym: 0.62}), spies() as rec:
                process_eval_job(orchestrator(), jobs[0])
            assert (tid, sym) in rec["analyze"], rec  # TechnicalRSIStrategy.analyze ran for the held lot
            orders = [o for o in rec["orders"] if o["symbol"] == sym]
            assert len(orders) == 1 and orders[0]["signal"] == "SELL_STOP_PARTIAL", rec
            assert orders[0]["source"] == "auto" and orders[0]["exit_source"] == "stop_loss"
            assert len(auto_orders(sym)) == 1

    @pytest.mark.parametrize("tid", TENANTS)
    def test_held_lot_non_stop_exit_still_analyzed(self, tid):
        from services.eval_queue_runtime import process_eval_job

        sym = f"CS2X{tid[:1].upper()}/USDT"
        with tenant(tid):
            seed_lot(sym, 2000.0, last_rsi=60.0, recent_high=1.30)
            with fake_queue():
                jobs = _enqueue_heartbeat_then_entry(sym, tid, "entry_15m_vol_spike", 15)
            with paper({sym: 1.30}, indicators=PROFIT_IND), spies() as rec:
                process_eval_job(orchestrator(), jobs[0])
            assert (tid, sym) in rec["analyze"], rec
            orders = [o for o in rec["orders"] if o["symbol"] == sym]
            assert len(orders) == 1, rec
            assert orders[0]["source"] == "auto" and orders[0]["exit_source"] not in (None, "", "stop_loss"), orders
            assert orders[0]["signal"].startswith("SELL")

    def test_entry_job_without_pending_heartbeat_keeps_entry_reason(self):
        """Not over-coalesced: no pending heartbeat (or a closed lot) -> the entry job stays an entry job."""
        from bus.eval_queue import enqueue_eval, pop_eval_batch

        with fake_queue():
            assert enqueue_eval("CS2E/USDT", TF, reason="entry_15m_vol_spike", priority=15, config_raw=QCFG)
            jobs = pop_eval_batch(5, config_raw=QCFG)
        assert [(j.symbol, j.reason) for j in jobs] == [("CS2E/USDT", "entry_15m_vol_spike")]

    def test_popped_heartbeat_does_not_capture_later_entry(self):
        """Meta hash outlives the pop: a heartbeat that already ran must not turn a later entry job into one."""
        from bus.eval_queue import enqueue_eval, pop_eval_batch

        with fake_queue():
            assert enqueue_eval("CS2Q/USDT", TF, reason="position_heartbeat", priority=30, config_raw=QCFG)
            assert [j.reason for j in pop_eval_batch(5, config_raw=QCFG)] == ["position_heartbeat"]
            assert enqueue_eval("CS2Q/USDT", TF, reason="entry_15m_vol_spike", priority=15, config_raw=QCFG)
            assert [j.reason for j in pop_eval_batch(5, config_raw=QCFG)] == ["entry_15m_vol_spike"]
