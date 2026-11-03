"""Mongo mirror for shadow/observe JSONL. The file write stays. Mongo does not block."""

from __future__ import annotations

import json
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from services.observe_streams import (
    STREAM_FILES,
    TTL_SECONDS,
    build_observe_document,
    mirror_jsonl_record,
    mirror_record,
    reset_for_tests,
    set_insert_hook_for_tests,
    stream_collections,
)
from services.observability_store import append_jsonl


@pytest.fixture(autouse=True)
def _mirror_on(monkeypatch):
    monkeypatch.setenv("OBSERVE_STREAMS_UNDER_TEST", "1")
    reset_for_tests()
    yield
    reset_for_tests()
    set_insert_hook_for_tests(None)


def _wait(predicate, timeout: float = 2.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_each_observe_stream_is_readable_from_its_collection():
    saved: list[tuple[str, dict]] = []

    def hook(collection, doc):
        saved.append((collection, doc))

    set_insert_hook_for_tests(hook)
    for filename, collection in STREAM_FILES.items():
        mirror_jsonl_record(
            f"/app/logs/{filename}",
            {
                "ts": "2026-10-31T00:00:00Z",
                "symbol": "SOL/USDT",
                "type": filename.replace(".jsonl", ""),
            },
        )
    assert _wait(lambda: len(saved) == len(STREAM_FILES))
    got = {collection for collection, _doc in saved}
    assert got == set(stream_collections())
    assert len(got) == 7
    by_collection = {collection: doc for collection, doc in saved}
    signal = by_collection["obs_shadow_signal"]
    link = by_collection["obs_shadow_link"]
    assert signal["type"] == "shadow_signal"
    assert link["type"] == "shadow_link"
    assert link["tenant_id"]
    assert isinstance(link["timestamp"], datetime)


def test_shadow_link_is_not_stored_as_shadow_signal():
    saved: list[tuple[str, str]] = []

    def hook(collection, doc):
        saved.append((collection, str(doc.get("type"))))

    set_insert_hook_for_tests(hook)
    mirror_jsonl_record(
        "/app/logs/shadow_link.jsonl",
        {
            "type": "shadow_link",
            "shadow_signal_id": "sig-1",
            "signal_id": "sig-1",
            "ts_utc": "2026-10-31T00:00:00.000000Z",
            "symbol": "SOL/USDT",
        },
    )
    assert _wait(lambda: saved)
    assert saved == [("obs_shadow_link", "shadow_link")]
    assert "obs_shadow_signal" not in {collection for collection, _kind in saved}


def test_blocking_mongo_insert_does_not_stall_append(tmp_path):
    gate = threading.Event()
    entered = threading.Event()

    def slow(_collection, _doc):
        entered.set()
        gate.wait(timeout=2)

    set_insert_hook_for_tests(slow)
    path = tmp_path / "buy_decision_tape.jsonl"
    started = time.perf_counter()
    append_jsonl(str(path), {"ts": "2026-10-31T00:00:00Z", "symbol": "SOL/USDT", "tenant": "henry"})
    elapsed = time.perf_counter() - started
    assert elapsed < 0.25
    assert path.read_text(encoding="utf-8").strip()
    assert entered.wait(1.0)
    again = time.perf_counter()
    append_jsonl(str(path), {"ts": "2026-10-31T00:00:01Z", "symbol": "ADA/USDT", "tenant": "henry"})
    assert time.perf_counter() - again < 0.25
    gate.set()
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [row["symbol"] for row in rows] == ["SOL/USDT", "ADA/USDT"]


def test_full_queue_drops_without_blocking():
    from services import observe_streams as obs

    gate = threading.Event()
    entered = threading.Event()
    previous = obs._queue

    def slow(_collection, _doc):
        entered.set()
        gate.wait(timeout=2)

    set_insert_hook_for_tests(slow)
    obs._queue = queue.Queue(maxsize=1)
    try:
        mirror_record("obs_buy_decision_tape", {"ts": "2026-10-31T00:00:00Z", "symbol": "SOL/USDT"})
        assert entered.wait(1.0)
        mirror_record("obs_buy_decision_tape", {"ts": "2026-10-31T00:00:01Z", "symbol": "ADA/USDT"})
        started = time.perf_counter()
        mirror_record("obs_buy_decision_tape", {"ts": "2026-10-31T00:00:02Z", "symbol": "DOT/USDT"})
        assert time.perf_counter() - started < 0.25
    finally:
        gate.set()
        obs._queue = previous


def test_mongo_error_does_not_escape_and_jsonl_remains(tmp_path):
    seen: list[str] = []

    def hook(_collection, doc):
        seen.append(str(doc.get("symbol")))
        if doc.get("symbol") == "BAD/USDT":
            raise RuntimeError("mongo down")

    set_insert_hook_for_tests(hook)
    path = tmp_path / "cap_order_observe.jsonl"
    append_jsonl(str(path), {"ts": "2026-10-31T00:00:00Z", "symbol": "BAD/USDT"})
    append_jsonl(str(path), {"ts": "2026-10-31T00:00:01Z", "symbol": "GOOD/USDT"})
    assert _wait(lambda: "GOOD/USDT" in seen)
    text = path.read_text(encoding="utf-8")
    assert "BAD/USDT" in text and "GOOD/USDT" in text


def test_ttl_and_tenant_indexes():
    from services.observe_streams import _ensure_indexes, _indexed

    created: list[tuple] = []

    class Coll:
        def create_index(self, key, **kwargs):
            created.append((key, kwargs))

    class DB:
        def __getitem__(self, _name):
            return Coll()

    _indexed.clear()
    _ensure_indexes(DB(), "obs_buy_decision_tape")
    _ensure_indexes(DB(), "obs_buy_decision_tape")
    ttl = [kwargs for key, kwargs in created if kwargs.get("expireAfterSeconds")]
    assert len(ttl) == 1
    assert ttl[0]["expireAfterSeconds"] == TTL_SECONDS
    assert TTL_SECONDS == 120 * 24 * 60 * 60
    assert any(key == "tenant_id" for key, _kwargs in created)
    assert any(key == "timestamp" for key, _kwargs in created)


def test_document_uses_tenant_and_ts():
    doc = build_observe_document(
        {"ts": "2026-10-31T01:02:03Z", "tenant": "henry", "symbol": "SOL/USDT"}
    )
    assert doc["tenant_id"] == "henry"
    assert doc["timestamp"] == datetime(2026, 10, 31, 1, 2, 3, tzinfo=timezone.utc)
    assert doc["symbol"] == "SOL/USDT"


def test_docs_name_every_collection_and_a_read_only_query():
    text = Path("docs/observability/observe-streams.md").read_text(encoding="utf-8")
    for collection in stream_collections():
        assert collection in text
    assert "find(" in text
    assert "120" in text
    assert "read-only" in text.lower() or "read only" in text.lower()


# --- BSON int64 overflow (obs_shadow_control "can only handle up to 8-byte ints") ---

from hypothesis import given, settings
from hypothesis import strategies as st

from services.observe_streams import BSON_INT64_MAX, BSON_INT64_MIN, bson_safe_value


def _bson_encode(doc: dict) -> bytes:
    bson = pytest.importorskip("bson")
    return bson.encode(doc)


def test_control_seed_above_int64_is_stored_as_exact_string():
    seed = 2**64 - 1
    doc = build_observe_document(
        {"type": "shadow_control", "ts_utc": "2026-11-02T20:59:26Z", "control_seed": seed}
    )
    assert doc["control_seed"] == str(seed)
    assert int(doc["control_seed"]) == seed
    _bson_encode(doc)  # raised OverflowError before the fix


def test_int64_boundaries_stay_ints_and_one_past_becomes_string():
    doc = build_observe_document(
        {
            "max": BSON_INT64_MAX,
            "min": BSON_INT64_MIN,
            "over": BSON_INT64_MAX + 1,
            "under": BSON_INT64_MIN - 1,
            "flag": True,
            "price": 1.5,
        }
    )
    assert doc["max"] == BSON_INT64_MAX and isinstance(doc["max"], int)
    assert doc["min"] == BSON_INT64_MIN and isinstance(doc["min"], int)
    assert doc["over"] == str(BSON_INT64_MAX + 1)
    assert doc["under"] == str(BSON_INT64_MIN - 1)
    assert doc["flag"] is True
    assert doc["price"] == 1.5
    _bson_encode(doc)


def test_nested_big_ints_are_converted_and_record_is_not_mutated():
    record = {"ts": "2026-11-02T00:00:00Z", "outer": {"seeds": [2**63, (2**70, 3)]}}
    doc = build_observe_document(record)
    assert doc["outer"]["seeds"] == [str(2**63), [str(2**70), 3]]
    assert record["outer"]["seeds"][0] == 2**63  # JSONL row keeps the number
    _bson_encode(doc)


def test_real_shadow_control_seed_reaches_the_insert_hook_as_bson():
    """A signal UUID with the high bit set gives a seed >= 2**63 (the prod failure)."""
    from services.shadow_signal_log import control_seed_from_signal_id

    signal_id = "ffffffff-ffff-4fff-bfff-ffffffffffff"
    seed = control_seed_from_signal_id(signal_id)
    assert seed > BSON_INT64_MAX
    saved: list[tuple[str, dict]] = []
    set_insert_hook_for_tests(lambda c, d: saved.append((c, d)))
    mirror_jsonl_record(
        "/app/logs/shadow_control.jsonl",
        {"type": "shadow_control", "signal_id": signal_id, "control_seed": seed, "ts_utc": "2026-11-03T07:07:33Z"},
    )
    assert _wait(lambda: len(saved) == 1)
    collection, doc = saved[0]
    assert collection == "obs_shadow_control"
    assert doc["control_seed"] == str(seed)
    _bson_encode(doc)


_json_like = st.recursive(
    st.none() | st.booleans() | st.integers(min_value=-(2**80), max_value=2**80) | st.floats(allow_nan=False) | st.text(max_size=5),
    lambda children: st.lists(children, max_size=4) | st.dictionaries(st.text(min_size=1, max_size=4).filter(lambda k: "\x00" not in k and not k.startswith("$") and "." not in k), children, max_size=4),
    max_leaves=20,
)


@settings(max_examples=200, deadline=None)
@given(_json_like)
def test_property_bson_safe_value_always_encodes_and_keeps_in_range_ints(value):
    safe = bson_safe_value({"v": value})
    _bson_encode(safe)

    def check(orig, new):
        if isinstance(orig, bool):
            assert new is orig
        elif isinstance(orig, int):
            if BSON_INT64_MIN <= orig <= BSON_INT64_MAX:
                assert new == orig and isinstance(new, int)
            else:
                assert new == str(orig)
        elif isinstance(orig, dict):
            assert set(new) == set(orig)
            for k in orig:
                check(orig[k], new[k])
        elif isinstance(orig, (list, tuple)):
            assert len(new) == len(orig)
            for a, b in zip(orig, new):
                check(a, b)
        else:
            assert new == orig

    check(value, safe["v"])
