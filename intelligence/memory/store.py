"""Mongo-backed Trading Memory store.

LEDGER SAFETY: only writes to memory_* collections.
Never touches orders, positions, trade_history, or cash collections.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from intelligence.memory.models import CoinProfile, Lesson, MarketEvent, TradeMemory
from logger import log

# Dedicated collections — isolated from ledger SOT
COL_PROFILES = "memory_coin_profiles"
COL_EVENTS = "memory_market_events"
COL_TRADES = "memory_trades"
COL_LESSONS = "memory_lessons"

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


def _db():
    from storage.mongo_client import get_database

    return get_database()


def _assert_safe_collection(name: str) -> None:
    if name in _FORBIDDEN or not name.startswith("memory_"):
        raise RuntimeError(f"memory store refused collection: {name}")


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
        ledger_scope: str = "live",
        tenant_id: str = "default",
    ) -> CoinProfile | None:
        try:
            _id = f"{tenant_id}|{ledger_scope}|{symbol}"
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
    ) -> list[MarketEvent]:
        q: dict[str, Any] = {}
        if symbol:
            sym = symbol.upper()
            q["symbols"] = {"$in": [sym, sym.split("/")[0], f"{sym.split('/')[0]}/USDT"]}
        if event_type:
            q["event_type"] = event_type
        if since_iso:
            q["timestamp"] = {"$gte": since_iso}
        try:
            cur = self._col(COL_EVENTS).find(q).sort("timestamp", -1).limit(int(limit))
            out = []
            for doc in cur:
                e = MarketEvent.from_doc(doc)
                if e:
                    out.append(e)
            return out
        except Exception as e:
            log(f"memory list_events failed: {e}", "WARNING")
            return []

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
            self._col(COL_TRADES).create_index([("symbol", 1), ("tenant_id", 1)])
            self._col(COL_LESSONS).create_index([("created_at", -1)])
        except Exception as e:
            log(f"memory ensure_indexes: {e}", "DEBUG")


# In-memory store for unit tests (no Mongo)
class InMemoryMemoryStore(MemoryStore):
    def __init__(self):
        super().__init__(db=None)
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

    def get_profile(self, symbol, *, ledger_scope="live", tenant_id="default"):
        return CoinProfile.from_doc(
            self._docs[COL_PROFILES].get(f"{tenant_id}|{ledger_scope}|{symbol}")
        )

    def list_profiles(self, *, tenant_id="default", limit=200):
        rows = [CoinProfile.from_doc(d) for d in self._docs[COL_PROFILES].values()]
        return [p for p in rows if p and p.tenant_id == tenant_id][:limit]

    def upsert_event(self, event: MarketEvent) -> bool:
        return self._put(COL_EVENTS, event.to_doc())

    def get_event(self, event_id: str):
        return MarketEvent.from_doc(self._docs[COL_EVENTS].get(event_id))

    def list_events(self, *, symbol=None, event_type=None, since_iso=None, limit=50):
        out = []
        for d in self._docs[COL_EVENTS].values():
            e = MarketEvent.from_doc(d)
            if not e:
                continue
            if event_type and e.event_type != event_type:
                continue
            if since_iso and e.timestamp < since_iso:
                continue
            if symbol:
                base = symbol.split("/")[0].upper()
                if not any(base in s.upper() or s.upper() == symbol.upper() for s in e.symbols):
                    continue
            out.append(e)
        out.sort(key=lambda x: x.timestamp, reverse=True)
        return out[:limit]

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
