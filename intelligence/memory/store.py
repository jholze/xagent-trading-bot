"""Mongo-backed Trading Memory store.

LEDGER SAFETY: only writes to memory_* collections.
Never touches orders, positions, trade_history, or cash collections.
"""

from __future__ import annotations

import copy
import os
import threading
from typing import Any, Callable

from intelligence.memory.models import (
    GLOBAL_TENANT_ID,
    CoinProfile,
    Lesson,
    MarketEvent,
    TradeMemory,
    memory_timestamp_unix,
    normalize_memory_timestamp,
)
from logger import log

# Dedicated collections — isolated from ledger SOT
COL_PROFILES = "memory_coin_profiles"
COL_EVENTS = "memory_market_events"
COL_TRADES = "memory_trades"
COL_LESSONS = "memory_lessons"
# Path-stats (episode backtests) — see intelligence.memory.path_stats
# COL_PATH_STATS = "memory_path_stats" (owned by path_stats module)

_FORBIDDEN = frozenset(
    {
        "orders",
        "positions",
        "trade_history",
        "compound_orders",
        "compound_positions",
        "compound_trade_history",
    }
)


def memory_enabled(config: dict | None = None) -> bool:
    if os.environ.get("MEMORY_ENABLED", "").strip().lower() in ("0", "false", "no", "off"):
        return False
    try:
        if config is None:
            from core.config import get_bot_config

            config = get_bot_config().raw
        mem = (config or {}).get("memory") or {}
        if "enabled" in mem:
            return bool(mem.get("enabled"))
    except Exception:
        pass
    return True


def event_visible_to_tenant(event_tenant: str | None, tenant_id: str | None) -> bool:
    """True when ``tenant_id`` may see this event.

    ``None`` keeps the unfiltered read used by paths that are not a tenant pack.
    A pack sees its own rows plus market-wide ``global`` rows.
    """
    if not tenant_id:
        return True
    return (event_tenant or "") in (tenant_id, GLOBAL_TENANT_ID)


def event_tenant_filter(tenant_id: str | None) -> dict[str, Any]:
    """Mongo clause for :func:`event_visible_to_tenant`. Empty when unfiltered."""
    if not tenant_id:
        return {}
    return {"tenant_id": {"$in": [tenant_id, GLOBAL_TENANT_ID]}}


def resolve_memory_scope(explicit: str | None = None) -> str:
    """Match CoinProfile keys to the active ledger (demo on Railway staging, live otherwise).

    Rebuild writes profiles as `{tenant}|{resolve_ledger_scope()}|{symbol}`. Callers that
    hardcode `live` miss demo profiles and skip size_bias updates.
    """
    if explicit:
        return str(explicit)
    try:
        from data_manager import resolve_ledger_scope

        return resolve_ledger_scope() or "live"
    except Exception:
        return "live"


def _db():
    from storage.mongo_client import get_database

    return get_database()


def _assert_safe_collection(name: str) -> None:
    if name in _FORBIDDEN or not name.startswith("memory_"):
        raise RuntimeError(f"memory store refused collection: {name}")


# --- #822 coalesced DCA skip family (first / latest rows) -------------------


def _skip_reason_key(code: Any) -> str:
    """Mongo field-safe reason code for ``metadata.reason_counts``."""
    return str(code).replace(".", "_").replace("$", "_") or "_"


def _get_path(doc: dict, path: str) -> Any:
    cur: Any = doc
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _set_path(doc: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    cur = doc
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _doc_matches(doc: dict, flt: dict) -> bool:
    for key, cond in flt.items():
        val = _get_path(doc, key)
        if isinstance(cond, dict) and any(str(k).startswith("$") for k in cond):
            for op, ref in cond.items():
                if val is None:
                    return False
                if op == "$gte" and not val >= ref:
                    return False
                if op == "$lt" and not val < ref:
                    return False
        elif val != cond:
            return False
    return True


class _DictEventCollection:
    """The few Mongo collection calls ``record_coalesced_skip`` needs, over a dict.

    Each call is atomic under one lock, like a single-document Mongo write.
    """

    def __init__(self, docs: dict[str, dict], lock):
        self._docs = docs
        self._lock = lock

    def find_one(self, flt: dict) -> dict | None:
        with self._lock:
            doc = self._docs.get(flt.get("_id"))
            if doc is None or not _doc_matches(doc, flt):
                return None
            return copy.deepcopy(doc)

    def insert_one(self, doc: dict) -> None:
        from pymongo.errors import DuplicateKeyError

        with self._lock:
            if doc["_id"] in self._docs:
                raise DuplicateKeyError(f"duplicate _id {doc['_id']}")
            self._docs[doc["_id"]] = copy.deepcopy(doc)

    def replace_one(self, flt: dict, doc: dict):
        with self._lock:
            cur = self._docs.get(flt.get("_id"))
            ok = cur is not None and _doc_matches(cur, flt)
            if ok:
                self._docs[flt["_id"]] = copy.deepcopy(doc)
            return _Result(1 if ok else 0)

    def delete_one(self, flt: dict):
        with self._lock:
            cur = self._docs.get(flt.get("_id"))
            ok = cur is not None and _doc_matches(cur, flt)
            if ok:
                del self._docs[flt["_id"]]
            return _Result(1 if ok else 0)

    def update_one(self, flt: dict, update: dict):
        with self._lock:
            return _Result(1 if self._apply(flt, update) is not None else 0)

    def find_one_and_update(self, flt: dict, update: dict, return_document=None):
        del return_document  # always returns the document after the update
        with self._lock:
            doc = self._apply(flt, update)
            return copy.deepcopy(doc) if doc is not None else None

    def _apply(self, flt: dict, update: dict) -> dict | None:
        doc = self._docs.get(flt.get("_id"))
        if doc is None or not _doc_matches(doc, flt):
            return None
        for path, inc in (update.get("$inc") or {}).items():
            _set_path(doc, path, (_get_path(doc, path) or 0) + inc)
        for path, val in (update.get("$set") or {}).items():
            _set_path(doc, path, val)
        return doc


class _Result:
    def __init__(self, n: int):
        self.matched_count = n
        self.deleted_count = n
        self.modified_count = n


def _skip_family_description(text: str, meta: dict) -> str:
    return (
        f"{text} count={meta.get('count')} first_ts={meta.get('first_ts')} "
        f"last_ts={meta.get('last_ts')}"
    )[:2000]


class MemoryStore:
    """CRUD for Trading Memory entities. Fail-open on missing Mongo in read paths."""

    def __init__(self, db=None):
        self._db = db

    @property
    def db(self):
        if self._db is not None:
            return self._db
        return _db()

    def _col(self, name: str):
        _assert_safe_collection(name)
        return self.db[name]

    # --- profiles ---
    def upsert_profile(self, profile: CoinProfile) -> bool:
        try:
            doc = profile.to_doc()
            self._col(COL_PROFILES).replace_one({"_id": doc["_id"]}, doc, upsert=True)
            return True
        except Exception as e:
            log(f"memory upsert_profile failed: {e}", "WARNING")
            return False

    def get_profile(
        self,
        symbol: str,
        *,
        ledger_scope: str | None = None,
        tenant_id: str = "default",
    ) -> CoinProfile | None:
        scope = resolve_memory_scope(ledger_scope)
        try:
            _id = f"{tenant_id}|{scope}|{symbol}"
            return CoinProfile.from_doc(self._col(COL_PROFILES).find_one({"_id": _id}))
        except Exception as e:
            log(f"memory get_profile failed: {e}", "DEBUG")
            return None

    def list_profiles(
        self, *, tenant_id: str = "default", limit: int = 200
    ) -> list[CoinProfile]:
        try:
            cur = (
                self._col(COL_PROFILES)
                .find({"tenant_id": tenant_id})
                .sort("as_of", -1)
                .limit(int(limit))
            )
            out = []
            for doc in cur:
                p = CoinProfile.from_doc(doc)
                if p:
                    out.append(p)
            return out
        except Exception as e:
            log(f"memory list_profiles failed: {e}", "WARNING")
            return []

    # --- events ---
    def upsert_event(self, event: MarketEvent) -> bool:
        try:
            doc = event.to_doc()
            self._col(COL_EVENTS).replace_one({"_id": doc["_id"]}, doc, upsert=True)
            return True
        except Exception as e:
            log(f"memory upsert_event failed: {e}", "WARNING")
            return False

    def _events_col(self):
        return self._col(COL_EVENTS)

    def record_coalesced_skip(
        self,
        event: MarketEvent,
        *,
        first_id: str,
        latest_id: str,
        reset_before_unix: float | None = None,
    ) -> tuple[str, bool]:
        """Count one DCA skip into its family's ``first`` / ``latest`` rows (#822).

        ``event`` is the plain skip (description, metadata, tenant, timestamp).
        Writes are single-document atomic: ``first`` is inserted once, ``latest``
        is incremented with ``$inc``; no read-modify-write of the count. A family
        whose newest row is older than ``reset_before_unix`` starts over.
        Returns ``(event_id, row_created)``. Raises on any store error so the
        caller can write nothing instead of overwriting audit fields.
        """
        from pymongo import ReturnDocument
        from pymongo.errors import DuplicateKeyError

        col = self._events_col()
        base = event.to_doc()
        tid = base.get("tenant_id")
        ts = base["timestamp"]
        ts_unix = base["ts_unix"]
        text = str(base.get("description") or "")
        meta = dict(base.get("metadata") or {})
        codes = [str(c) for c in (meta.get("reason_codes") or [])]
        inc_reasons = {f"metadata.reason_counts.{_skip_reason_key(c)}": 1 for c in codes}

        def _first_doc() -> dict:
            m = {
                **meta,
                "slot": "first",
                "count": 1,
                "first_ts": ts,
                "last_ts": ts,
                "reason_counts": {_skip_reason_key(c): 1 for c in codes},
            }
            return {
                **base,
                "_id": first_id,
                "event_id": first_id,
                "metadata": m,
                "description": _skip_family_description(text, m),
            }

        fresh = {"ts_unix": {"$gte": reset_before_unix}} if reset_before_unix else {}
        set_latest = {
            "timestamp": ts,
            "ts_unix": ts_unix,
            "impact_score": base.get("impact_score"),
            "metadata.last_ts": ts,
        }
        for k, v in meta.items():
            if k not in ("reason_counts", "count", "first_ts", "last_ts", "slot"):
                set_latest[f"metadata.{k}"] = v

        for _attempt in range(4):
            # 1) Fresh latest row: atomic increment.
            doc = col.find_one_and_update(
                {"_id": latest_id, "tenant_id": tid, **fresh},
                {"$inc": {"metadata.count": 1, **inc_reasons}, "$set": set_latest},
                return_document=ReturnDocument.AFTER,
            )
            if doc is not None:
                n = (doc.get("metadata") or {}).get("count")
                # Cosmetic text; only the writer whose count is still current wins.
                col.update_one(
                    {"_id": latest_id, "metadata.count": n},
                    {"$set": {"description": _skip_family_description(text, doc["metadata"])}},
                )
                return latest_id, False
            # 2) No family yet: insert the first row.
            try:
                col.insert_one(_first_doc())
                return first_id, True
            except DuplicateKeyError:
                pass
            first = col.find_one({"_id": first_id})
            if first is None:
                continue  # reset by another writer; retry
            if first.get("tenant_id") != tid:
                raise RuntimeError(f"skip family {first_id} belongs to another tenant")
            latest = col.find_one({"_id": latest_id})
            newest = latest if latest is not None else first
            if reset_before_unix and float(newest.get("ts_unix") or 0) < reset_before_unix:
                # 3) Stale family: start over. Guarded on the row we read.
                res = col.replace_one(
                    {"_id": first_id, "ts_unix": first.get("ts_unix")}, _first_doc()
                )
                if res.matched_count:
                    col.delete_one({"_id": latest_id, "ts_unix": {"$lt": reset_before_unix}})
                    return first_id, True
                continue
            if latest is not None:
                continue  # latest appeared or was refreshed meanwhile; retry step 1
            # 4) Second skip of the family: create latest from first.
            fm = first.get("metadata") or {}
            counts = dict(fm.get("reason_counts") or {})
            for c in codes:
                key = _skip_reason_key(c)
                counts[key] = int(counts.get(key) or 0) + 1
            lm = {
                **meta,
                "slot": "latest",
                "count": int(fm.get("count") or 1) + 1,
                "first_ts": fm.get("first_ts") or first.get("timestamp"),
                "last_ts": ts,
                "reason_counts": counts,
            }
            latest_doc = {
                **base,
                "_id": latest_id,
                "event_id": latest_id,
                "metadata": lm,
                "description": _skip_family_description(text, lm),
            }
            try:
                col.insert_one(latest_doc)
                return latest_id, True
            except DuplicateKeyError:
                continue
        raise RuntimeError(f"skip family {first_id}: too many concurrent retries")

    def get_event(self, event_id: str) -> MarketEvent | None:
        try:
            return MarketEvent.from_doc(self._col(COL_EVENTS).find_one({"_id": event_id}))
        except Exception:
            return None

    def list_events(
        self,
        *,
        symbol: str | None = None,
        event_type: str | None = None,
        since_iso: str | None = None,
        limit: int = 50,
        tenant_id: str | None = None,
    ) -> list[MarketEvent]:
        q: dict[str, Any] = {}
        q.update(event_tenant_filter(tenant_id))
        if symbol:
            sym = symbol.upper()
            q["symbols"] = {"$in": [sym, sym.split("/")[0], f"{sym.split('/')[0]}/USDT"]}
        if event_type:
            q["event_type"] = event_type
        # Do not filter/sort on raw timestamp strings: RFC822 pubDates sort by weekday
        # name and beat ISO-8601 lexicographically (#782). Prefer ts_unix.
        since_u = memory_timestamp_unix(since_iso) if since_iso else None
        if since_u is not None and since_u > 0:
            q["ts_unix"] = {"$gte": since_u}
        lim = max(1, int(limit))
        try:
            # Two-arg sort matches pymongo and unit fakes; Python re-sort below
            # is the chronological source of truth once rows are loaded (#782).
            cur = (
                self._col(COL_EVENTS)
                .find(q)
                .sort("ts_unix", -1)
                .limit(lim)
            )
            out = []
            for doc in cur:
                e = MarketEvent.from_doc(doc)
                if e:
                    out.append(e)
            # Legacy rows without ts_unix sort as null (last). If the window is short,
            # over-fetch unindexed rows and rank by parsed time so MCP is not stuck.
            # No Mongo .sort() here: RFC822 strings are not chronological, and the
            # pre-backfill window is unordered by design — Python sort below heals it
            # once enough rows are fetched; Hermes backfill then fills ts_unix (#782).
            if len(out) < lim:
                q_legacy = dict(q)
                q_legacy.pop("ts_unix", None)
                q_legacy["ts_unix"] = {"$exists": False}
                need = lim - len(out)
                legacy = []
                for doc in self._col(COL_EVENTS).find(q_legacy).limit(max(need * 40, 200)):
                    e = MarketEvent.from_doc(doc)
                    if not e:
                        continue
                    if since_u is not None and memory_timestamp_unix(e.timestamp) < since_u:
                        continue
                    legacy.append(e)
                legacy.sort(key=lambda x: memory_timestamp_unix(x.timestamp), reverse=True)
                seen = {e.event_id for e in out}
                for e in legacy:
                    if e.event_id in seen:
                        continue
                    out.append(e)
                    seen.add(e.event_id)
                    if len(out) >= lim:
                        break
            out.sort(key=lambda x: memory_timestamp_unix(x.timestamp), reverse=True)
            return out[:lim]
        except Exception as e:
            log(f"memory list_events failed: {e}", "WARNING")
            return []

    def backfill_event_timestamps(self, *, batch: int = 2000) -> dict[str, int]:
        """Rewrite RFC822 / missing ts_unix on memory_market_events (#782). Ledger-safe.

        RFC822 weekday strings sort *above* ISO-8601 lexicographically (``Tue`` >
        ``2026-…``). Until those rows are rewritten, MCP images still on the old
        ``.sort("timestamp")`` path keep returning a mid-2026 stale window even
        after fresh ISO events are ingested. Always drain RFC822 rows first.
        """
        batch = max(1, min(10000, int(batch)))
        normalized = 0
        # ``rfc822`` counts RFC822 rows queued for rewrite in phase 1;
        # ``normalized`` counts rows actually written (logging only).
        rfc822 = 0
        try:
            col = self._col(COL_EVENTS)
            # Phase 1: weekday RFC822 pubDates (must win over generic missing-ts).
            queries = [
                ("rfc822", {"timestamp": {"$regex": "^[A-Za-z]"}}),
                (
                    "missing_ts_unix",
                    {
                        "ts_unix": {"$exists": False},
                        "timestamp": {"$not": {"$regex": "^[A-Za-z]"}},
                    },
                ),
            ]
            ops: list[tuple[dict, dict]] = []
            for label, query in queries:
                if len(ops) >= batch:
                    break
                remain = batch - len(ops)
                for doc in col.find(
                    query, projection={"timestamp": 1, "ts_unix": 1}
                ).limit(remain):
                    raw = doc.get("timestamp")
                    iso = normalize_memory_timestamp(raw)
                    unix = memory_timestamp_unix(iso)
                    if doc.get("timestamp") == iso and doc.get("ts_unix") == unix:
                        continue
                    ops.append(
                        (
                            {"_id": doc["_id"]},
                            {"$set": {"timestamp": iso, "ts_unix": unix}},
                        )
                    )
                    if label == "rfc822":
                        rfc822 += 1
                    if len(ops) >= batch:
                        break
            if not ops:
                return {"normalized": 0, "rfc822": 0}
            try:
                from pymongo import UpdateOne

                col.bulk_write(
                    [UpdateOne(filt, patch) for filt, patch in ops],
                    ordered=False,
                )
                normalized = len(ops)
            except Exception:
                for filt, patch in ops:
                    col.update_one(filt, patch)
                    normalized += 1
        except Exception as e:
            log(f"memory backfill_event_timestamps failed: {e}", "WARNING")
            return {"normalized": normalized, "rfc822": rfc822, "error": 1}
        return {"normalized": normalized, "rfc822": rfc822}

    # --- trades ---
    def upsert_trade(self, trade: TradeMemory) -> bool:
        try:
            doc = trade.to_doc()
            self._col(COL_TRADES).replace_one({"_id": doc["_id"]}, doc, upsert=True)
            return True
        except Exception as e:
            log(f"memory upsert_trade failed: {e}", "WARNING")
            return False

    def list_trades(
        self,
        *,
        symbol: str | None = None,
        tenant_id: str = "default",
        limit: int = 200,
    ) -> list[TradeMemory]:
        q: dict[str, Any] = {"tenant_id": tenant_id}
        if symbol:
            q["symbol"] = symbol
        try:
            cur = self._col(COL_TRADES).find(q).sort("entry_time", -1).limit(int(limit))
            out = []
            for doc in cur:
                t = TradeMemory.from_doc(doc)
                if t:
                    out.append(t)
            return out
        except Exception as e:
            log(f"memory list_trades failed: {e}", "WARNING")
            return []

    # --- lessons ---
    def upsert_lesson(self, lesson: Lesson) -> bool:
        try:
            doc = lesson.to_doc()
            self._col(COL_LESSONS).replace_one({"_id": doc["_id"]}, doc, upsert=True)
            return True
        except Exception as e:
            log(f"memory upsert_lesson failed: {e}", "WARNING")
            return False

    def list_lessons(
        self,
        *,
        symbol: str | None = None,
        limit: int = 50,
    ) -> list[Lesson]:
        q: dict[str, Any] = {}
        if symbol:
            base = symbol.split("/")[0].upper()
            q["$or"] = [
                {"symbols": symbol},
                {"symbols": base},
                {"tags": base.lower()},
            ]
        try:
            cur = self._col(COL_LESSONS).find(q).sort("created_at", -1).limit(int(limit))
            out = []
            for doc in cur:
                les = Lesson.from_doc(doc)
                if les:
                    out.append(les)
            return out
        except Exception as e:
            log(f"memory list_lessons failed: {e}", "WARNING")
            return []

    def ensure_indexes(self) -> None:
        """Idempotent indexes — safe on shared DB."""
        try:
            self._col(COL_PROFILES).create_index([("symbol", 1), ("tenant_id", 1)])
            self._col(COL_EVENTS).create_index([("timestamp", -1)])
            self._col(COL_EVENTS).create_index([("event_type", 1), ("timestamp", -1)])
            self._col(COL_EVENTS).create_index([("ts_unix", -1)])
            self._col(COL_EVENTS).create_index([("event_type", 1), ("ts_unix", -1)])
            self._col(COL_TRADES).create_index([("symbol", 1), ("tenant_id", 1)])
            self._col(COL_LESSONS).create_index([("created_at", -1)])
        except Exception as e:
            log(f"memory ensure_indexes: {e}", "DEBUG")


# In-memory store for unit tests (no Mongo)
class InMemoryMemoryStore(MemoryStore):
    def __init__(self):
        super().__init__(db=None)
        self._events_lock = threading.Lock()
        self._docs: dict[str, dict[str, dict]] = {
            COL_PROFILES: {},
            COL_EVENTS: {},
            COL_TRADES: {},
            COL_LESSONS: {},
        }

    def _put(self, col: str, doc: dict) -> bool:
        self._docs[col][doc["_id"]] = dict(doc)
        return True

    def upsert_profile(self, profile: CoinProfile) -> bool:
        return self._put(COL_PROFILES, profile.to_doc())

    def get_profile(self, symbol, *, ledger_scope=None, tenant_id="default"):
        scope = resolve_memory_scope(ledger_scope)
        return CoinProfile.from_doc(
            self._docs[COL_PROFILES].get(f"{tenant_id}|{scope}|{symbol}")
        )

    def list_profiles(self, *, tenant_id="default", limit=200):
        rows = [CoinProfile.from_doc(d) for d in self._docs[COL_PROFILES].values()]
        return [p for p in rows if p and p.tenant_id == tenant_id][:limit]

    def upsert_event(self, event: MarketEvent) -> bool:
        return self._put(COL_EVENTS, event.to_doc())

    def _events_col(self):
        # Same record_coalesced_skip code path as Mongo, over the dict store.
        return _DictEventCollection(self._docs[COL_EVENTS], self._events_lock)

    def get_event(self, event_id: str):
        return MarketEvent.from_doc(self._docs[COL_EVENTS].get(event_id))

    def list_events(
        self, *, symbol=None, event_type=None, since_iso=None, limit=50, tenant_id=None
    ):
        out = []
        since_u = memory_timestamp_unix(since_iso) if since_iso else None
        for d in self._docs[COL_EVENTS].values():
            e = MarketEvent.from_doc(d)
            if not e:
                continue
            if not event_visible_to_tenant(e.tenant_id, tenant_id):
                continue
            if event_type and e.event_type != event_type:
                continue
            if since_u is not None and memory_timestamp_unix(e.timestamp) < since_u:
                continue
            if symbol:
                base = symbol.split("/")[0].upper()
                if not any(base in s.upper() or s.upper() == symbol.upper() for s in e.symbols):
                    continue
            out.append(e)
        out.sort(key=lambda x: memory_timestamp_unix(x.timestamp), reverse=True)
        return out[:limit]

    def backfill_event_timestamps(self, *, batch: int = 2000) -> dict[str, int]:
        normalized = 0
        rfc822 = 0
        # Prefer weekday RFC822 rows first (same priority as Mongo path).
        items = list(self._docs[COL_EVENTS].items())
        items.sort(
            key=lambda kv: (
                0 if str((kv[1] or {}).get("timestamp") or "")[:1].isalpha() else 1,
                kv[0],
            )
        )
        for _id, d in items:
            if normalized >= batch:
                break
            raw = d.get("timestamp")
            iso = normalize_memory_timestamp(raw)
            unix = memory_timestamp_unix(iso)
            if d.get("timestamp") == iso and d.get("ts_unix") == unix:
                continue
            was_rfc = str(raw or "")[:1].isalpha()
            d = dict(d)
            d["timestamp"] = iso
            d["ts_unix"] = unix
            self._docs[COL_EVENTS][_id] = d
            normalized += 1
            if was_rfc:
                rfc822 += 1
        return {"normalized": normalized, "rfc822": rfc822}

    def upsert_trade(self, trade: TradeMemory) -> bool:
        return self._put(COL_TRADES, trade.to_doc())

    def list_trades(self, *, symbol=None, tenant_id="default", limit=200):
        out = []
        for d in self._docs[COL_TRADES].values():
            t = TradeMemory.from_doc(d)
            if not t or t.tenant_id != tenant_id:
                continue
            if symbol and t.symbol != symbol:
                continue
            out.append(t)
        return out[:limit]

    def upsert_lesson(self, lesson: Lesson) -> bool:
        return self._put(COL_LESSONS, lesson.to_doc())

    def list_lessons(self, *, symbol=None, limit=50):
        out = [Lesson.from_doc(d) for d in self._docs[COL_LESSONS].values()]
        out = [x for x in out if x]
        if symbol:
            base = symbol.split("/")[0].upper()
            out = [
                L
                for L in out
                if base in [s.upper() for s in L.symbols]
                or base.lower() in [t.lower() for t in L.tags]
            ]
        return out[:limit]

    def ensure_indexes(self) -> None:
        return
