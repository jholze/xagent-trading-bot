"""Mirror shadow/observe JSONL rows into Mongo. Best-effort, never blocking.

One collection per stream. The JSONL file remains the local copy. A Mongo
outage, a slow insert, or a full queue is dropped on the caller side: the
trading cycle only enqueues. The worker thread talks to Mongo.

Collections are listed in ``STREAM_FILES``. ``shadow_link`` is not stored in
``obs_shadow_signal``.
"""

from __future__ import annotations

import os
import queue
import threading
from datetime import datetime, timezone
from typing import Any, Callable

TTL_DAYS = 120
TTL_SECONDS = TTL_DAYS * 24 * 60 * 60
_QUEUE_MAX = 8192

# Basename of the JSONL file → collection. Order is the documented order.
STREAM_FILES: dict[str, str] = {
    "buy_decision_tape.jsonl": "obs_buy_decision_tape",
    "daily_gainer_desk.jsonl": "obs_daily_gainer_desk",
    "cap_order_observe.jsonl": "obs_cap_order_observe",
    "shadow_signal.jsonl": "obs_shadow_signal",
    "shadow_exit.jsonl": "obs_shadow_exit",
    "shadow_link.jsonl": "obs_shadow_link",
    "shadow_control.jsonl": "obs_shadow_control",
}

_queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX)
_worker_lock = threading.Lock()
_worker_started = False
_indexed: set[str] = set()
_index_lock = threading.Lock()
_insert_hook: Callable[[str, dict], None] | None = None


def stream_collections() -> list[str]:
    return list(STREAM_FILES.values())


def collection_for_path(path: str) -> str | None:
    name = os.path.basename(str(path or ""))
    return STREAM_FILES.get(name)


def _enabled() -> bool:
    flag = (os.environ.get("OBSERVE_STREAMS_MONGO") or "").strip().lower()
    if flag in ("0", "false", "off"):
        return False
    if os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get("OBSERVE_STREAMS_UNDER_TEST") != "1":
        return False
    return True


def _resolve_tenant(record: dict) -> str:
    raw = record.get("tenant_id")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    tenant = record.get("tenant")
    if isinstance(tenant, str) and tenant.strip():
        return tenant.strip()
    try:
        from core.tenant_context import resolve_tenant_id

        return resolve_tenant_id()
    except Exception:
        return "default"


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed
    return datetime.now(timezone.utc)


def _timestamp_source(record: dict) -> Any:
    raw = record.get("timestamp")
    if isinstance(raw, datetime):
        return raw
    for key in ("ts", "ts_utc", "timestamp"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def build_observe_document(record: dict) -> dict:
    """Copy the JSONL object and add ``tenant_id`` plus a BSON ``timestamp``."""
    doc = dict(record)
    doc["tenant_id"] = _resolve_tenant(doc)
    doc["timestamp"] = _as_datetime(_timestamp_source(doc))
    return doc


def _ensure_indexes(db: Any, collection: str) -> None:
    with _index_lock:
        if collection in _indexed:
            return
    coll = db[collection]
    coll.create_index(
        "timestamp",
        expireAfterSeconds=TTL_SECONDS,
        name="obs_timestamp_ttl",
    )
    coll.create_index("tenant_id", name="obs_tenant_id")
    with _index_lock:
        _indexed.add(collection)


def _insert_mongo(collection: str, doc: dict) -> None:
    from storage.mongo_client import assert_safe_dev_db_mutation, get_database

    db = get_database()
    assert_safe_dev_db_mutation(db.name, action="write")
    _ensure_indexes(db, collection)
    db[collection].insert_one(doc)


def _insert_document(collection: str, doc: dict) -> None:
    hook = _insert_hook
    if hook is not None:
        hook(collection, doc)
        return
    # A worker started by a test must not open the operator database.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    _insert_mongo(collection, doc)


def _worker() -> None:
    while True:
        try:
            collection, doc = _queue.get(timeout=0.2)
        except queue.Empty:
            continue
        try:
            _insert_document(collection, doc)
        except Exception as exc:
            try:
                from logger import log

                log(f"observe stream insert failed ({collection}): {exc}", "WARNING")
            except Exception:
                pass
            continue


def _ensure_worker() -> None:
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        thread = threading.Thread(target=_worker, name="observe-streams", daemon=True)
        thread.start()
        _worker_started = True


def mirror_record(collection: str, record: dict) -> None:
    """Enqueue one document. Never raises. Never waits on Mongo."""
    if not collection or not isinstance(record, dict):
        return
    if not _enabled():
        return
    try:
        doc = build_observe_document(record)
        _ensure_worker()
        _queue.put_nowait((collection, doc))
    except queue.Full:
        return
    except Exception:
        return


def mirror_jsonl_record(path: str, record: dict) -> None:
    """Mirror a JSONL append when ``path`` is a known observe stream."""
    collection = collection_for_path(path)
    if collection is None:
        return
    mirror_record(collection, record)


def set_insert_hook_for_tests(hook: Callable[[str, dict], None] | None) -> None:
    global _insert_hook
    _insert_hook = hook


def reset_for_tests() -> None:
    """Drop queued docs and the insert hook. Does not join the daemon worker."""
    global _insert_hook
    _insert_hook = None
    with _index_lock:
        _indexed.clear()
    while True:
        try:
            _queue.get_nowait()
        except queue.Empty:
            break
