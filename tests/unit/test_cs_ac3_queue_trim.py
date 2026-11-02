"""AC3 (cycle-silence spec): a queue above eval_queue_max_len (repo 500, config.json:459) must not drop position
heartbeats. Base defect: bus/eval_queue.py:179-181 ZREMRANGEBYRANK <key> max_len -1 drops the HIGHEST scores;
score = priority*1e16 + ms (:97-99), heartbeat 30 > social 25 > entry 15 (:13-19), so heartbeats go before entry jobs.
Entry/stale jobs may be trimmed; heartbeats never. Then the surviving heartbeat drives the REAL process_eval_job.
"""
from __future__ import annotations

import pytest

from cs_helpers import CFG, QCFG, FakeRedis, fake_queue, orchestrator, paper, seed_lot, spies, tenant, TF

TENANTS = [pytest.param("default", id="default"), pytest.param("henry", id="henry")]
CAP = int(CFG["architecture"]["eval_queue_max_len"])  # 500


def _fill(n, tid, reason="entry_15m_vol_spike", pri=15, prefix="CS3E"):
    from bus.eval_queue import enqueue_eval

    for i in range(n):
        enqueue_eval(f"{prefix}{i:04d}/USDT", TF, reason=reason, priority=pri, config_raw=QCFG, tenant_id=tid)


def _members(r):
    return dict(r.z.get("cstest:eval:queue", {}))


def test_repo_cap():
    assert CAP == 500 and QCFG["architecture"]["eval_queue_max_len"] == CAP


class TestAC3HeartbeatNeverTrimmed:
    @pytest.mark.parametrize("tid", TENANTS)
    @pytest.mark.parametrize("order", ["heartbeat_first", "heartbeat_into_full_queue"])
    def test_heartbeat_survives_cap_and_drives_stop(self, tid, order):
        from bus.eval_queue import enqueue_eval, pop_eval_batch
        from services.eval_queue_runtime import process_eval_job

        sym = f"CS3H{tid[:1].upper()}{order[10:12].upper()}/USDT"
        with tenant(tid):
            seed_lot(sym, 2000.0)
            r = FakeRedis()
            with fake_queue(r):
                if order == "heartbeat_first":
                    assert enqueue_eval(sym, TF, reason="position_heartbeat", priority=30, config_raw=QCFG,
                                        tenant_id=tid)
                    _fill(CAP, tid)
                else:
                    _fill(CAP, tid)
                    enqueue_eval(sym, TF, reason="position_heartbeat", priority=30, config_raw=QCFG, tenant_id=tid)
                members = _members(r)
                hb = [m for m in members if sym in m]
                assert len(hb) == 1, (len(members), hb)  # the heartbeat member is still queued
                assert len(members) <= CAP + 1  # entry jobs were trimmed instead
                jobs = pop_eval_batch(CAP + 5, config_raw=QCFG)
            mine = [j for j in jobs if j.symbol == sym]
            assert len(mine) == 1 and mine[0].reason == "position_heartbeat"
            with paper({sym: 0.62}), spies() as rec:
                process_eval_job(orchestrator(), mine[0])
            orders = [o for o in rec["orders"] if o["symbol"] == sym]
            assert len(orders) == 1 and orders[0]["signal"] == "SELL_STOP_PARTIAL" and orders[0]["source"] == "auto"

    def test_many_heartbeats_all_kept_entry_trimmed(self):
        """30 held lots + 600 entry jobs: every heartbeat kept, total back to the cap, only entry jobs removed."""
        from bus.eval_queue import enqueue_eval

        r = FakeRedis()
        with fake_queue(r):
            for i in range(30):
                assert enqueue_eval(f"CS3K{i:02d}/USDT", TF, reason="position_heartbeat", priority=30, config_raw=QCFG)
            _fill(600, "default")
        members = _members(r)
        assert sum(1 for m in members if m.startswith("CS3K") or "|CS3K" in m) == 30
        assert len(members) == CAP

    def test_stale_jobs_trimmed_before_entry_jobs(self):
        from bus.eval_queue import enqueue_eval

        r = FakeRedis()
        with fake_queue(r):
            _fill(CAP, "default", reason="stale_watchlist", pri=50, prefix="CS3S")
            _fill(10, "default", prefix="CS3N")
        members = _members(r)
        assert len(members) == CAP and sum(1 for m in members if "CS3N" in m) == 10
