"""#822: identical DCA skip events must not fill the fact window.

AC-1 one hour of skips -> at most 2 skip rows per lot among the last 40 events,
count and last_ts kept, a market event from the same hour still visible.
AC-2 20 identical skips -> last skip time and count stored, guard still blocks.
AC-3 (reflector side) lives in tests/unit/test_dca_reflector.py.
Fix round 1 (PR #824 review): atomic store write (no read-modify-write), family
key tenant|symbol, reset after the coin-facts lookback, one RAG chunk per row.

The symbol is synthetic.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from core.tenant_context import tenant_context
from intelligence.memory.models import GLOBAL_TENANT_ID, MarketEvent
from intelligence.memory.rag_index import index_store_into_rag
from intelligence.memory.store import (
    COL_EVENTS,
    InMemoryMemoryStore,
    MemoryStore,
    _DictEventCollection,
)
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
        patch("strategies.dca_decision_event.skip_family_reset_hours", return_value=72.0),
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


def test_alternating_reason_sets_on_one_lot_stay_within_two_rows():
    """Review item 2: the family is the lot (tenant|symbol), not the reason set."""
    store = InMemoryMemoryStore()
    below = _skip_result()
    rounds = DcaPolicyResult(
        size_mult=1.0,
        skip=True,
        reason_codes=("dca_guard_below_avg", "dca_guard_max_rounds"),
    )
    harvest = DcaPolicyResult(size_mult=0.4, skip=True, reason_codes=("harvest_skip",))
    with _clock() as now:
        for i in range(30):
            now["ts"] = _iso(T0 + timedelta(minutes=2 * i))
            res, applied = [(below, "dca_guard"), (rounds, "dca_guard"), (harvest, "live_skip")][i % 3]
            assert _persist(store, "henry", result=res, applied=applied)
    window = store.list_events(symbol=SYM, tenant_id="henry", limit=FACT_WINDOW)
    rows = _skip_rows(window)
    assert len(rows) <= SKIP_EVENTS_PER_LOT, [e.event_id for e in rows]
    latest = next(e for e in rows if e.metadata["slot"] == "latest")
    assert latest.metadata["count"] == 30
    assert latest.metadata["reason_counts"] == {
        "dca_guard_below_avg": 20,
        "dca_guard_max_rounds": 10,
        "harvest_skip": 10,
    }
    # The latest row shows the newest skip (i = 29 -> harvest via live_skip).
    assert latest.metadata["reason_codes"] == ["harvest_skip"]
    assert latest.metadata["applied"] == "live_skip"


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


class _FlakyCollection(_DictEventCollection):
    """Mongo-like collection; selected calls raise like a dropped connection."""

    def __init__(self, docs, fail: set[str]):
        import threading

        super().__init__(docs, threading.Lock())
        self.fail = fail

    def _maybe(self, name):
        if name in self.fail:
            raise RuntimeError(f"mongo {name} timed out")

    def find_one(self, flt):
        self._maybe("find_one")
        return super().find_one(flt)

    def find_one_and_update(self, flt, update, return_document=None):
        self._maybe("find_one_and_update")
        return super().find_one_and_update(flt, update, return_document)

    def insert_one(self, doc):
        self._maybe("insert_one")
        return super().insert_one(doc)

    def update_one(self, flt, update):
        self._maybe("update_one")
        return super().update_one(flt, update)


def _real_store(docs, fail=()):
    """The production MemoryStore class over a Mongo-like collection."""
    return MemoryStore(db={COL_EVENTS: _FlakyCollection(docs, set(fail))})


def _snapshot(docs):
    import copy

    return copy.deepcopy(docs)


def test_read_failure_in_real_store_does_not_overwrite_first_or_reset_count():
    """Review item 1: a failed read must not rewrite first_ts or reset count."""
    docs: dict[str, dict] = {}
    with _clock() as now:
        for i in range(5):
            now["ts"] = _iso(T0 + timedelta(minutes=i))
            assert _persist(_real_store(docs), "henry")
        before = _snapshot(docs)
        # Latest row exists: the increment itself fails.
        for fail in (
            {"find_one_and_update"},
            {"find_one_and_update", "find_one"},
            {"insert_one", "find_one_and_update"},
        ):
            now["ts"] = _iso(T0 + timedelta(minutes=30))
            assert _persist(_real_store(docs, fail), "henry") == ""
            assert docs == before, f"store changed after failing {fail}"
        # Recovery: next skip continues the same family.
        now["ts"] = _iso(T0 + timedelta(minutes=40))
        assert _persist(_real_store(docs), "henry")
    rows = {d["metadata"]["slot"]: d for d in docs.values()}
    assert rows["first"]["metadata"]["first_ts"] == _iso(T0)
    assert rows["latest"]["metadata"]["count"] == 6
    assert rows["latest"]["metadata"]["first_ts"] == _iso(T0)


def test_real_store_read_failure_with_only_first_row_keeps_first():
    """Only ``first`` exists: the read of it fails -> nothing rewritten."""
    docs: dict[str, dict] = {}
    with _clock() as now:
        assert _persist(_real_store(docs), "henry")
        before = _snapshot(docs)
        now["ts"] = _iso(T0 + timedelta(minutes=5))
        assert _persist(_real_store(docs, {"find_one"}), "henry") == ""
    assert docs == before
    assert next(iter(docs.values()))["metadata"]["first_ts"] == _iso(T0)


def test_real_store_read_failure_on_first_skip_writes_nothing():
    docs: dict[str, dict] = {}
    with _clock():
        assert _persist(_real_store(docs, {"find_one_and_update"}), "henry") == ""
    assert docs == {}


def test_interleaved_writers_do_not_lose_increments():
    """Review item 1/LOW: dca.py and the sniper writing the same lot at once."""
    docs: dict[str, dict] = {}

    class Interleave(_FlakyCollection):
        """Run writer B in full right before writer A inserts the latest row."""

        hooked = False

        def insert_one(self, doc):
            if doc["_id"].endswith("_latest") and not Interleave.hooked:
                Interleave.hooked = True
                assert _persist(MemoryStore(db={COL_EVENTS: self}), "henry")
            return super().insert_one(doc)

    with _clock() as now:
        coll = Interleave(docs, set())
        store = MemoryStore(db={COL_EVENTS: coll})
        now["ts"] = _iso(T0)
        assert _persist(store, "henry")  # first
        now["ts"] = _iso(T0 + timedelta(minutes=1))
        assert _persist(store, "henry")  # A; B runs inside A
    latest = docs[next(k for k in docs if k.endswith("_latest"))]
    assert latest["metadata"]["count"] == 3
    assert len(docs) == 2


def test_threaded_writers_count_every_skip():
    import threading

    store = InMemoryMemoryStore()
    with _clock():
        def writer():
            for _ in range(50):
                assert _persist(store, "henry")

        threads = [threading.Thread(target=writer) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    rows = _skip_rows(store.list_events(symbol=SYM, tenant_id="henry", limit=100))
    assert len(rows) == 2
    assert max(e.metadata["count"] for e in rows) == 100


def test_family_restarts_after_lookback_hours():
    """Review item 3: count / first_ts reset when the family is older than lookback."""
    store = InMemoryMemoryStore()
    with _clock() as now:
        for i in range(5):
            now["ts"] = _iso(T0 + timedelta(minutes=i))
            _persist(store, "henry")
        # 71 h later: same family continues.
        now["ts"] = _iso(T0 + timedelta(hours=71))
        _persist(store, "henry")
        rows = _skip_rows(store.list_events(symbol=SYM, tenant_id="henry", limit=100))
        assert max(e.metadata["count"] for e in rows) == 6
        # 72 h + 1 min after the newest skip: new family.
        restart = _iso(T0 + timedelta(hours=143, minutes=1))
        now["ts"] = restart
        _persist(store, "henry")
    rows = _skip_rows(store.list_events(symbol=SYM, tenant_id="henry", limit=100))
    assert len(rows) == 1
    first = rows[0]
    assert first.metadata["slot"] == "first"
    assert first.metadata["count"] == 1
    assert first.metadata["first_ts"] == restart


def test_reset_hours_read_from_coin_facts_lookback():
    from strategies.dca_decision_event import skip_family_reset_hours

    with patch(
        "intelligence.memory.coin_facts.coin_facts_config",
        return_value={"lookback_hours": 336},
    ):
        assert skip_family_reset_hours() == 336.0
    with patch(
        "intelligence.memory.coin_facts.coin_facts_config", side_effect=RuntimeError("x")
    ):
        assert skip_family_reset_hours() == 72.0


def test_dedupe_failure_does_not_raise_or_write_and_guard_still_blocks():
    class BoomStore(InMemoryMemoryStore):
        def record_coalesced_skip(self, *a, **k):
            raise RuntimeError("mongo down")

    store = BoomStore()
    with _clock(), _guard_inputs_fresh():
        eid = _persist(store, "henry")
        skip = policy_skip_for_guard(LOT, PRICE_BELOW_AVG, symbol=SYM)
    assert eid == ""
    assert store.list_events() == []
    assert skip is not None and skip.skip is True
    assert skip.reason_codes == ("dca_guard_below_avg",)


def test_each_skip_row_has_exactly_one_live_rag_chunk():
    """Review item 4: the cycle indexer owns the chunk; the writer adds none."""
    from hermes.memory.rag_retriever import RagRetriever

    cfg = {"memory": {"rag": {"enabled": True, "embedding_backend": "hash", "use_weaviate_rag": False}}}
    rag = RagRetriever.in_memory(config=cfg)
    store = InMemoryMemoryStore()
    writer_calls: list[dict] = []

    class SpyRag:
        def __init__(self, *a, **k):
            pass

        def add_to_memory(self, text, meta):
            writer_calls.append(dict(meta))
            return "rag_x"

    with (
        _clock() as now,
        patch("hermes.memory.rag_retriever.RagRetriever", SpyRag),
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
            index_store_into_rag(store, rag, config=cfg)
    assert writer_calls == []
    chunks = [c for c in rag._store.list_chunks(limit=500) if "dca_decision" in c.text]
    assert len(chunks) == 2, [c.text for c in chunks]
    assert {c.metadata["source_id"] for c in chunks} == {
        e.event_id for e in _skip_rows(store.list_events(symbol=SYM, limit=10))
    }
    latest_chunk = next(c for c in chunks if c.metadata["source_id"].endswith("_latest"))
    assert "count=10" in latest_chunk.text


def test_code_default_is_n2():
    from strategies import dca_decision_event

    assert dca_decision_event.SKIP_EVENTS_PER_LOT == SKIP_EVENTS_PER_LOT
