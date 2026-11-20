"""#822: identical DCA skip events must not fill the fact window.

AC-1 one hour of skips -> at most 2 skip rows per lot among the last 40 events,
count and last_ts kept, a market event from the same hour still visible.
AC-2 20 identical skips -> last skip time and count stored, guard still blocks.
AC-3 (reflector side) lives in tests/unit/test_dca_reflector.py.

The symbol is synthetic.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from core.tenant_context import tenant_context
from intelligence.memory.models import GLOBAL_TENANT_ID, MarketEvent
from intelligence.memory.store import InMemoryMemoryStore
from strategies.dca_decision_event import persist_dca_decision_event
from strategies.dca_policy import (
    DcaPolicyResult,
    emit_dca_policy_audit,
    evaluate_dca_guard,
    policy_skip_for_guard,
)

SYM = "LABX/USDT"
SKIP_EVENTS_PER_LOT = 2  # N = 2 (Kay, #822)
FACT_WINDOW = 40  # rule_switches.pre_trade_gate.facts.max_events in config.json
T0 = datetime(2026, 11, 12, 8, 0, 0, tzinfo=timezone.utc)
LOT = {"symbol": SYM, "average_entry": 1.00, "dca_rounds": 0, "amount": 100.0}
PRICE_BELOW_AVG = 0.90


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _skip_result() -> DcaPolicyResult:
    return DcaPolicyResult(size_mult=1.0, skip=True, reason_codes=("dca_guard_below_avg",))


@contextmanager
def _clock():
    """Settable clock for _utc_now_iso; memory writes enabled."""
    now = {"ts": _iso(T0)}
    with (
        patch("strategies.dca_decision_event._utc_now_iso", lambda: now["ts"]),
        patch("intelligence.memory.store.memory_enabled", return_value=True),
    ):
        yield now


@contextmanager
def _guard_inputs_fresh():
    """Guard reads price staleness and the lock doc; pin both to 'ok'."""
    with (
        patch("strategies.dca_policy._price_is_stale", return_value=False),
        patch("strategies.dca_policy._lock_state", return_value=False),
    ):
        yield


def _persist(store, tenant: str, result: DcaPolicyResult | None = None, applied="dca_guard"):
    with tenant_context(tenant, scope="paper"):
        return persist_dca_decision_event(
            symbol=SYM,
            result=result or _skip_result(),
            applied=applied,
            policy_cfg={"persist_events": True, "index_rag": False},
            store=store,
            index_rag=False,
        )


def _skip_rows(events: list[MarketEvent]) -> list[MarketEvent]:
    return [
        e
        for e in events
        if e.event_type == "dca_decision" and (e.metadata or {}).get("skip")
    ]


def test_ac1_one_hour_of_skips_takes_at_most_two_slots_in_fact_window():
    store = InMemoryMemoryStore()
    # 30 older market events for the symbol (before the hour).
    for i in range(30):
        store.upsert_event(
            MarketEvent(
                event_id=f"old_{i}",
                timestamp=_iso(T0 - timedelta(hours=3) + timedelta(minutes=i)),
                event_type="news",
                symbols=[SYM],
                tenant_id=GLOBAL_TENANT_ID,
            )
        )
    breakout = MarketEvent(
        event_id="mkt_breakout",
        timestamp=_iso(T0 + timedelta(minutes=5, seconds=30)),
        event_type="volume_breakout",
        symbols=[SYM],
        tenant_id=GLOBAL_TENANT_ID,
    )
    store.upsert_event(breakout)

    # Skip every 90 s for one hour = 40 identical skips.
    stamps = [_iso(T0 + timedelta(seconds=90 * i)) for i in range(40)]
    with _clock() as now:
        for ts in stamps:
            now["ts"] = ts
            assert _persist(store, "henry")

    window = store.list_events(symbol=SYM, tenant_id="henry", limit=FACT_WINDOW)
    skips = _skip_rows(window)
    assert len(skips) <= SKIP_EVENTS_PER_LOT, [e.event_id for e in skips]
    latest = max(skips, key=lambda e: int(e.metadata.get("count") or 0))
    assert latest.metadata["count"] == 40
    assert latest.metadata["last_ts"] == stamps[-1]
    assert latest.metadata["first_ts"] == stamps[0]
    assert "mkt_breakout" in {e.event_id for e in window}


def test_ac2_twenty_skips_keep_count_and_last_ts_and_guard_still_blocks():
    store = InMemoryMemoryStore()
    stamps = [_iso(T0 + timedelta(minutes=3 * i)) for i in range(20)]
    decisions = []
    with (
        _clock() as now,
        _guard_inputs_fresh(),
        patch("intelligence.memory.store.MemoryStore", return_value=store),
        tenant_context("henry", scope="paper"),
    ):
        for ts in stamps:
            now["ts"] = ts
            guard = evaluate_dca_guard(
                LOT, price=PRICE_BELOW_AVG, source="dca", has_open_lot=True, symbol=SYM
            )
            skip = policy_skip_for_guard(LOT, PRICE_BELOW_AVG, symbol=SYM)
            decisions.append((guard.blocked, guard.code, skip))
            # Same call the DCA add path makes on a guard skip (strategies/dca.py).
            emit_dca_policy_audit(
                symbol=SYM,
                result=skip,
                shadow=False,
                base_usdt=100.0,
                final_usdt=0.0,
                applied="dca_guard",
                policy_cfg={"persist_events": True, "index_rag": False, "log_audit": False},
            )

    # Guard decision: blocked every time, same code, same policy skip.
    assert all(d[0] is True for d in decisions)
    assert {d[1] for d in decisions} == {"dca_guard_below_avg"}
    assert {d[2] for d in decisions} == {
        DcaPolicyResult(size_mult=1.0, skip=True, reason_codes=("dca_guard_below_avg",))
    }

    rows = _skip_rows(store.list_events(symbol=SYM, tenant_id="henry", limit=100))
    assert len(rows) == 2
    by_slot = {e.metadata["slot"]: e for e in rows}
    assert by_slot["first"].metadata["count"] == 1
    assert by_slot["first"].timestamp == stamps[0]
    assert by_slot["latest"].metadata["count"] == 20
    assert by_slot["latest"].metadata["last_ts"] == stamps[-1]
    assert by_slot["latest"].metadata["first_ts"] == stamps[0]
    assert by_slot["latest"].timestamp == stamps[-1]
    assert "count=20" in by_slot["latest"].description


def test_dedupe_is_tenant_scoped():
    store = InMemoryMemoryStore()
    with _clock() as now:
        for i in range(6):
            now["ts"] = _iso(T0 + timedelta(minutes=i))
            _persist(store, "henry")
        for i in range(3):
            now["ts"] = _iso(T0 + timedelta(minutes=10 + i))
            _persist(store, "default")

    henry = _skip_rows(store.list_events(symbol=SYM, tenant_id="henry", limit=100))
    default = _skip_rows(store.list_events(symbol=SYM, tenant_id="default", limit=100))
    assert {e.tenant_id for e in henry} == {"henry"}
    assert {e.tenant_id for e in default} == {"default"}
    assert max(e.metadata["count"] for e in henry) == 6
    assert max(e.metadata["count"] for e in default) == 3
    assert not {e.event_id for e in henry} & {e.event_id for e in default}


def test_different_reason_sets_are_separate_families():
    store = InMemoryMemoryStore()
    other = DcaPolicyResult(
        size_mult=1.0,
        skip=True,
        reason_codes=("dca_guard_below_avg", "dca_guard_max_rounds"),
    )
    with _clock() as now:
        for i in range(5):
            now["ts"] = _iso(T0 + timedelta(minutes=i))
            _persist(store, "henry")
            _persist(store, "henry", result=other)
    rows = _skip_rows(store.list_events(symbol=SYM, tenant_id="henry", limit=100))
    keys = {e.metadata["coalesce_key"] for e in rows}
    assert len(keys) == 2
    assert len(rows) == 4


def test_applied_decisions_are_still_one_row_each():
    store = InMemoryMemoryStore()
    applied = DcaPolicyResult(size_mult=1.0, skip=False, reason_codes=("steady",))
    with _clock() as now:
        for i in range(5):
            now["ts"] = _iso(T0 + timedelta(minutes=i))
            assert _persist(store, "henry", result=applied, applied="live")
    rows = [
        e
        for e in store.list_events(symbol=SYM, tenant_id="henry", limit=100)
        if e.event_type == "dca_decision"
    ]
    assert len(rows) == 5
    assert all(not e.metadata.get("skip") for e in rows)
    assert all("coalesce_key" not in e.metadata for e in rows)


def test_dedupe_failure_does_not_raise_or_write_and_guard_still_blocks():
    class BoomStore(InMemoryMemoryStore):
        def get_event(self, event_id):
            raise RuntimeError("mongo down")

    store = BoomStore()
    with _clock(), _guard_inputs_fresh():
        eid = _persist(store, "henry")
        skip = policy_skip_for_guard(LOT, PRICE_BELOW_AVG, symbol=SYM)
    assert eid == ""
    assert store.list_events() == []
    assert skip is not None and skip.skip is True
    assert skip.reason_codes == ("dca_guard_below_avg",)


def test_new_slot_indexes_rag_once_with_stable_chunk_key():
    store = InMemoryMemoryStore()
    calls: list[dict] = []

    class FakeRag:
        def add_to_memory(self, text, meta):
            calls.append(dict(meta))
            return "rag_x"

    with (
        _clock() as now,
        patch("hermes.memory.rag_retriever.RagRetriever", FakeRag),
        patch("intelligence.memory.rag_config.rag_enabled", return_value=True),
        tenant_context("henry", scope="paper"),
    ):
        for i in range(10):
            now["ts"] = _iso(T0 + timedelta(minutes=i))
            persist_dca_decision_event(
                symbol=SYM,
                result=_skip_result(),
                applied="dca_guard",
                policy_cfg={"persist_events": True, "index_rag": True},
                store=store,
            )
    # One chunk for the first slot, one when the latest slot is created.
    assert len(calls) == 2
    assert all(c["chunk_key"] == c["source_id"] for c in calls)


def test_code_default_is_n2():
    from strategies import dca_decision_event

    assert dca_decision_event.SKIP_EVENTS_PER_LOT == SKIP_EVENTS_PER_LOT
