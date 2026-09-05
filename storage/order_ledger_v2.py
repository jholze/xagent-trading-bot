"""Order ledger v2: one document per order + materialised day stats.

Hot paths (day list / day stats) query by tenant+scope+day_key and never load
the legacy unbounded orders[] blob. Writes dual-update this store; legacy blob
dual-write remains in OrderService until full cutover.
"""

from __future__ import annotations

import copy
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Protocol

logger = logging.getLogger(__name__)

from core.tenant_context import DEFAULT_TENANT, resolve_tenant_id

ORDERS_V2_COLLECTION = "orders_v2"
DAY_STATS_COLLECTION = "order_day_stats"

# Blocked statuses mirrored from order_service (avoid circular import).
BLOCKED_STATUSES = frozenset({
    "rejected",
    "cancelled",
    "failed",
    "expired",
    "pending_confirmation",
    "executing",
})

_STORE_LOCK = threading.RLock()
_STORE: "OrderLedgerV2 | None" = None
# Index/connect failure must not pin a process-lifetime empty Memory store (#318).
_V2_DEGRADED = False


def order_ledger_v2_is_degraded() -> bool:
    """True when mongo v2 failed to initialise; stats_day_filled_fast must fail-closed."""
    return _V2_DEGRADED


def order_ledger_v2_enabled() -> bool:
    raw = (os.environ.get("ORDER_LEDGER_V2") or "1").strip().lower()
    return raw not in ("0", "false", "off", "no")


def order_ledger_v2_reads_enabled() -> bool:
    """Prefer v2 for day list/stats only when explicitly enabled.

    Default **off** so partial dual-write (new fills only) cannot hide
    historical blob day activity before backfill. Tests set
    ``ORDER_LEDGER_V2_READS=1``.
    """
    if not order_ledger_v2_enabled():
        return False
    raw = (os.environ.get("ORDER_LEDGER_V2_READS") or "0").strip().lower()
    return raw in ("1", "true", "on", "yes")


def order_ledger_v2_backfill_complete() -> bool:
    """When true, day reads may trust v2 alone (no blob union).

    Set ``ORDER_LEDGER_V2_BACKFILL_COMPLETE=1`` after historical orders are
    migrated into v2. Until then, day list/stats union v2 with legacy day
    window so blob-only same-day fills are not dropped.
    """
    raw = (os.environ.get("ORDER_LEDGER_V2_BACKFILL_COMPLETE") or "0").strip().lower()
    return raw in ("1", "true", "on", "yes")


def reset_order_ledger_v2_for_tests() -> None:
    """Drop process singleton (tests)."""
    global _STORE, _V2_DEGRADED
    with _STORE_LOCK:
        _STORE = None
        _V2_DEGRADED = False


def get_order_ledger_v2() -> "OrderLedgerV2 | None":
    """Return active v2 store, or None if disabled."""
    global _STORE
    if not order_ledger_v2_enabled():
        return None
    with _STORE_LOCK:
        if _STORE is not None:
            return _STORE
        backend = (os.environ.get("ORDER_LEDGER_V2_BACKEND") or "auto").strip().lower()
        if backend == "memory":
            _STORE = MemoryOrderLedgerV2()
        elif backend == "mongo":
            _STORE = _mongo_v2_or_unavailable()
        else:
            # auto: memory under pytest; mongo when demo ledger is mongo (Railway)
            under_pytest = bool(
                os.environ.get("PYTEST_CURRENT_TEST")
                or os.environ.get("PYTEST_RUNNING")
            )
            use_mongo = (
                not under_pytest
                and (
                    os.environ.get("DEMO_LEDGER_BACKEND") == "mongo"
                    or bool(os.environ.get("MONGO_URL"))
                )
            )
            if use_mongo:
                _STORE = _mongo_v2_or_unavailable()
            else:
                _STORE = MemoryOrderLedgerV2()
        return _STORE


def _mongo_v2_or_unavailable() -> "MongoOrderLedgerV2":
    """Build mongo v2 store; index failure is degraded, not a silent memory pin."""
    global _V2_DEGRADED
    from storage.errors import LedgerUnavailable

    store = MongoOrderLedgerV2()
    try:
        store.ensure_indexes()
    except Exception as e:
        _V2_DEGRADED = True
        logger.warning(
            "order ledger v2 mongo indexes failed; not pinning memory store: %s", e
        )
        raise LedgerUnavailable(op="get_order_ledger_v2", cause=e) from e
    _V2_DEGRADED = False
    return store


# ---------------------------------------------------------------------------
# Pure helpers (no I/O)
# ---------------------------------------------------------------------------


def compound_order_id(tenant_id: str, scope: str, order_id: str) -> str:
    return f"{tenant_id}:{scope}:{order_id}"


def compound_day_stats_id(tenant_id: str, scope: str, day_key: str) -> str:
    return f"{tenant_id}:{scope}:{day_key}"


def _parse_to_display_naive(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
    except Exception:
        return None
    try:
        from core.time_utils import display_tz

        target = display_tz()
    except Exception:
        target = None
    if dt.tzinfo is None:
        local_tz = datetime.now().astimezone().tzinfo
        if local_tz is not None:
            dt = dt.replace(tzinfo=local_tz)
        elif target is not None:
            dt = dt.replace(tzinfo=target)
        else:
            return dt
    if target is None:
        return dt.replace(tzinfo=None)
    try:
        return dt.astimezone(target).replace(tzinfo=None)
    except Exception:
        return dt.replace(tzinfo=None)


def order_event_ts_naive(order: dict) -> datetime | None:
    ts = order.get("timestamps") or {}
    status = (order.get("status") or "").lower()
    if status == "filled":
        return _parse_to_display_naive(ts.get("filled") or ts.get("created") or ts.get("updated"))
    return _parse_to_display_naive(ts.get("created") or ts.get("updated") or ts.get("filled"))


def display_day_key_for_order(order: dict) -> str:
    """Calendar day key in display TZ (YYYY-MM-DD) for list/stats windows."""
    if order.get("day_key"):
        return str(order["day_key"])
    dt = order_event_ts_naive(order)
    if dt is None:
        try:
            from core.time_utils import now_display

            n = now_display()
            if n.tzinfo is not None:
                n = n.replace(tzinfo=None)
            return n.strftime("%Y-%m-%d")
        except Exception:
            return datetime.now().strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m-%d")


def display_day_key_now() -> str:
    try:
        from core.time_utils import now_display

        n = now_display()
        if n.tzinfo is not None:
            n = n.replace(tzinfo=None)
        return n.strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


def order_notional_usdt(order: dict) -> float:
    exe = order.get("execution") or {}
    req = order.get("request") or {}
    for bag in (exe, req):
        try:
            usdt = float(bag.get("usdt") or 0)
        except (TypeError, ValueError):
            usdt = 0.0
        if usdt > 0:
            return usdt
    try:
        price = float(exe.get("price") or req.get("price") or 0)
        amount = float(exe.get("amount") or req.get("amount") or 0)
    except (TypeError, ValueError):
        return 0.0
    if price > 0 and amount > 0:
        return price * amount
    return 0.0


def empty_day_stats(
    tenant_id: str,
    scope: str,
    day_key: str,
) -> dict:
    return {
        "tenant_id": tenant_id,
        "ledger_scope": scope,
        "day_key": day_key,
        "filled": 0,
        "buys": 0,
        "sells": 0,
        "shorts": 0,
        "covers": 0,
        "buy_usdt": 0.0,
        "sell_usdt": 0.0,
        "short_usdt": 0.0,
        "cover_usdt": 0.0,
        "realized_pnl": 0.0,
        "sell_wins": 0,
        "sell_losses": 0,
        "wins": 0,
        "losses": 0,
        "unknown_side": 0,
        "blocked": 0,
        "blocked_by_status": {},
    }


# side -> (count_key, usdt_key, contributes_realized_pnl)
_SIDE_STATS: dict[str, tuple[str, str, bool]] = {
    "buy": ("buys", "buy_usdt", False),
    "sell": ("sells", "sell_usdt", True),
    "short": ("shorts", "short_usdt", False),
    "cover": ("covers", "cover_usdt", True),
}


def _token(value: object | None) -> str:
    """Lowercase status/side token. Safe for str, Enum, int. None → empty."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip().lower()
    inner = getattr(value, "value", None)
    if inner is not None and inner is not value:
        return _token(inner)
    try:
        return str(value).strip().lower()
    except Exception:
        return ""


def _order_ref(order: dict) -> object:
    return order.get("id") or order.get("display_seq") or "?"


def _parse_order_pnl(order: dict) -> float:
    """Sell/cover realized pnl. Invalid values log and count as 0.0."""
    raw = order.get("pnl")
    if raw is None:
        return 0.0
    if isinstance(raw, dict):
        raw = raw.get("usdt", raw.get("realized", raw.get("pnl")))
        if raw is None:
            return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning("order pnl parse failed id=%s raw=%r", _order_ref(order), raw)
        return 0.0


def stats_from_filled_orders(orders: Iterable[dict]) -> dict:
    """Aggregate filled-trade counts, notionals, and realized PnL.

    Included: status ``filled``, or a *missing* status (``None`` / absent key).
    ``""``, ``0``, and any other present value are not filled.

    Per filled order:
      buys / sells / shorts / covers — known-side counts
      *_usdt — notional from execution/request
      realized_pnl — sum of sell + cover pnl
      sell_wins / sell_losses — sell pnl sign only (legacy)
      wins / losses — sell + cover pnl sign
      unknown_side — filled orders whose side is not in the mapping
        (so filled == buys+sells+shorts+covers+unknown_side)
    """
    counts: dict[str, int | float] = {
        "filled": 0,
        "buys": 0,
        "sells": 0,
        "shorts": 0,
        "covers": 0,
        "buy_usdt": 0.0,
        "sell_usdt": 0.0,
        "short_usdt": 0.0,
        "cover_usdt": 0.0,
        "realized_pnl": 0.0,
        "sell_wins": 0,
        "sell_losses": 0,
        "wins": 0,
        "losses": 0,
        "unknown_side": 0,
    }
    for o in orders:
        if not isinstance(o, dict):
            continue
        raw_status = o.get("status")
        if raw_status is not None and _token(raw_status) != "filled":
            continue
        counts["filled"] += 1
        side = _token(o.get("side"))
        spec = _SIDE_STATS.get(side)
        notional = order_notional_usdt(o)
        if spec is None:
            counts["unknown_side"] += 1
            logger.warning(
                "order unknown side id=%s side=%r",
                _order_ref(o),
                o.get("side"),
            )
            continue
        count_key, usdt_key, has_pnl = spec
        counts[count_key] += 1
        counts[usdt_key] += notional
        if not has_pnl:
            continue
        pnl = _parse_order_pnl(o)
        counts["realized_pnl"] += pnl
        if pnl > 0:
            counts["wins"] += 1
            if side == "sell":
                counts["sell_wins"] += 1
        elif pnl < 0:
            counts["losses"] += 1
            if side == "sell":
                counts["sell_losses"] += 1
    return counts


def enrich_order_record(order: dict) -> dict:
    """Copy order with denormalised day_key / ts fields for v2 storage."""
    rec = copy.deepcopy(order)
    tid = str(rec.get("tenant_id") or resolve_tenant_id())
    scope = str(rec.get("ledger_scope") or "demo")
    oid = str(rec.get("id") or "")
    rec["tenant_id"] = tid
    rec["ledger_scope"] = scope
    rec["day_key"] = display_day_key_for_order(rec)
    ts = order_event_ts_naive(rec)
    if ts is not None:
        rec["ts_event"] = ts.isoformat()
    if oid:
        rec["_id"] = compound_order_id(tid, scope, oid)
    return rec


def public_order_view(doc: dict) -> dict:
    """Strip storage-only keys for API compatibility with legacy order dicts."""
    out = copy.deepcopy(doc)
    out.pop("_id", None)
    out.pop("ts_event", None)
    return out


# ---------------------------------------------------------------------------
# Store protocol
# ---------------------------------------------------------------------------


class OrderLedgerV2(Protocol):
    def upsert_order(self, order: dict) -> None: ...
    def get_by_id(self, tenant_id: str, scope: str, order_id: str) -> dict | None: ...
    def get_by_display_seq(self, tenant_id: str, scope: str, display_seq: int) -> dict | None: ...
    def has_tenant_orders(self, tenant_id: str, scope: str) -> bool: ...
    def query_day(
        self,
        tenant_id: str,
        scope: str,
        day_key: str,
        *,
        status_filter: set[str] | None = None,
        filled_only: bool = False,
        blocked_only: bool = False,
        limit: int = 500,
    ) -> list[dict]: ...
    def get_day_stats(self, tenant_id: str, scope: str, day_key: str) -> dict: ...
    def rebuild_day_stats(self, tenant_id: str, scope: str, day_key: str) -> dict: ...
    def full_blob_load_count(self) -> int: ...
    def ensure_indexes(self) -> None: ...


# ---------------------------------------------------------------------------
# Memory backend (tests + fallback)
# ---------------------------------------------------------------------------


@dataclass
class MemoryOrderLedgerV2:
    """In-process per-order store with day index — never loads a full history blob."""

    _orders: dict[str, dict] = field(default_factory=dict)
    _day_index: dict[tuple[str, str, str], list[str]] = field(default_factory=dict)
    _display_index: dict[tuple[str, str, int], str] = field(default_factory=dict)
    _day_stats: dict[str, dict] = field(default_factory=dict)
    _blob_loads: int = 0
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def full_blob_load_count(self) -> int:
        return self._blob_loads

    def ensure_indexes(self) -> None:
        return None

    def upsert_order(self, order: dict) -> None:
        rec = enrich_order_record(order)
        oid = str(rec.get("id") or "")
        if not oid:
            return
        tid = rec["tenant_id"]
        scope = rec["ledger_scope"]
        doc_id = compound_order_id(tid, scope, oid)
        with self._lock:
            old = self._orders.get(doc_id)
            if old:
                old_day = str(old.get("day_key") or "")
                old_key = (tid, scope, old_day)
                if old_key in self._day_index and doc_id in self._day_index[old_key]:
                    self._day_index[old_key] = [x for x in self._day_index[old_key] if x != doc_id]
                old_seq = old.get("display_seq")
                if old_seq is not None:
                    self._display_index.pop((tid, scope, int(old_seq)), None)

            self._orders[doc_id] = rec
            day = str(rec.get("day_key") or display_day_key_for_order(rec))
            dkey = (tid, scope, day)
            self._day_index.setdefault(dkey, [])
            if doc_id not in self._day_index[dkey]:
                self._day_index[dkey].append(doc_id)
            seq = rec.get("display_seq")
            if seq is not None:
                self._display_index[(tid, scope, int(seq))] = doc_id
            # Keep day_stats rebuildable from day index (source of truth = orders).
            self.rebuild_day_stats(tid, scope, day)

    def get_by_id(self, tenant_id: str, scope: str, order_id: str) -> dict | None:
        doc = self._orders.get(compound_order_id(tenant_id, scope, order_id))
        return public_order_view(doc) if doc else None

    def get_by_display_seq(self, tenant_id: str, scope: str, display_seq: int) -> dict | None:
        doc_id = self._display_index.get((tenant_id, scope, int(display_seq)))
        if not doc_id:
            return None
        doc = self._orders.get(doc_id)
        return public_order_view(doc) if doc else None

    def has_tenant_orders(self, tenant_id: str, scope: str) -> bool:
        prefix = f"{tenant_id}:{scope}:"
        return any(k.startswith(prefix) for k in self._orders)

    def query_day(
        self,
        tenant_id: str,
        scope: str,
        day_key: str,
        *,
        status_filter: set[str] | None = None,
        filled_only: bool = False,
        blocked_only: bool = False,
        limit: int = 500,
    ) -> list[dict]:
        # Hot path: only day index — never iterates all orders.
        ids = list(self._day_index.get((tenant_id, scope, day_key), []))
        out: list[dict] = []
        for doc_id in reversed(ids):  # newest-ish last-appended
            doc = self._orders.get(doc_id)
            if not doc:
                continue
            st = (doc.get("status") or "").lower()
            if filled_only and st != "filled":
                continue
            if blocked_only and st not in BLOCKED_STATUSES:
                continue
            if status_filter is not None and st not in status_filter:
                continue
            out.append(public_order_view(doc))
            if len(out) >= max(1, limit):
                break
        return out

    def get_day_stats(self, tenant_id: str, scope: str, day_key: str) -> dict:
        sid = compound_day_stats_id(tenant_id, scope, day_key)
        with self._lock:
            stats = self._day_stats.get(sid)
            if stats:
                return copy.deepcopy(stats)
        return self.rebuild_day_stats(tenant_id, scope, day_key)

    def rebuild_day_stats(self, tenant_id: str, scope: str, day_key: str) -> dict:
        filled = self.query_day(
            tenant_id, scope, day_key, filled_only=True, limit=10_000,
        )
        stats = empty_day_stats(tenant_id, scope, day_key)
        stats.update(stats_from_filled_orders(filled))
        blocked = self.query_day(
            tenant_id, scope, day_key, blocked_only=True, limit=10_000,
        )
        stats["blocked"] = len(blocked)
        by_st: dict[str, int] = {}
        for o in blocked:
            st = (o.get("status") or "").lower()
            by_st[st] = by_st.get(st, 0) + 1
        stats["blocked_by_status"] = by_st
        stats["updated_at"] = datetime.now().isoformat()
        sid = compound_day_stats_id(tenant_id, scope, day_key)
        with self._lock:
            self._day_stats[sid] = copy.deepcopy(stats)
        return copy.deepcopy(stats)


# ---------------------------------------------------------------------------
# Mongo backend
# ---------------------------------------------------------------------------


class MongoOrderLedgerV2:
    """Mongo per-order collection + day_stats docs."""

    def __init__(self, *, test: bool = False, config: dict | None = None):
        self._test = test
        self._config = config
        self._blob_loads = 0

    def full_blob_load_count(self) -> int:
        return self._blob_loads

    def _db(self):
        from storage.mongo_client import get_database

        return get_database(test=self._test, config=self._config)

    def ensure_indexes(self) -> None:
        from storage.mongo_client import assert_safe_dev_db_mutation, resolve_database_name

        assert_safe_dev_db_mutation(
            resolve_database_name(test=self._test, config=self._config),
            action="write",
        )
        oc = self._db()[ORDERS_V2_COLLECTION]
        oc.create_index(
            [("tenant_id", 1), ("ledger_scope", 1), ("day_key", 1), ("ts_event", -1)],
            name="tenant_scope_day_ts",
        )
        oc.create_index(
            [("tenant_id", 1), ("ledger_scope", 1), ("status", 1), ("day_key", 1)],
            name="tenant_scope_status_day",
        )
        oc.create_index(
            [("tenant_id", 1), ("ledger_scope", 1), ("display_seq", 1)],
            name="tenant_scope_display_seq",
            unique=True,
        )
        oc.create_index(
            [("idempotency_key", 1)],
            name="idempotency_key",
            unique=True,
            sparse=True,
        )
        self._db()[DAY_STATS_COLLECTION].create_index(
            [("tenant_id", 1), ("ledger_scope", 1), ("day_key", 1)],
            name="tenant_scope_day",
            unique=True,
        )

    def upsert_order(self, order: dict) -> None:
        from storage.mongo_client import assert_safe_dev_db_mutation, resolve_database_name

        assert_safe_dev_db_mutation(
            resolve_database_name(test=self._test, config=self._config),
            action="write",
        )
        rec = enrich_order_record(order)
        oid = str(rec.get("id") or "")
        if not oid:
            return
        self._db()[ORDERS_V2_COLLECTION].replace_one(
            {"_id": rec["_id"]}, rec, upsert=True,
        )
        self.rebuild_day_stats(rec["tenant_id"], rec["ledger_scope"], rec["day_key"])

    def get_by_id(self, tenant_id: str, scope: str, order_id: str) -> dict | None:
        doc = self._db()[ORDERS_V2_COLLECTION].find_one(
            {"_id": compound_order_id(tenant_id, scope, order_id)}
        )
        return public_order_view(doc) if doc else None

    def get_by_display_seq(self, tenant_id: str, scope: str, display_seq: int) -> dict | None:
        doc = self._db()[ORDERS_V2_COLLECTION].find_one(
            {
                "tenant_id": tenant_id,
                "ledger_scope": scope,
                "display_seq": int(display_seq),
            }
        )
        return public_order_view(doc) if doc else None

    def has_tenant_orders(self, tenant_id: str, scope: str) -> bool:
        return (
            self._db()[ORDERS_V2_COLLECTION].find_one(
                {"tenant_id": tenant_id, "ledger_scope": scope},
                projection={"_id": 1},
            )
            is not None
        )

    def query_day(
        self,
        tenant_id: str,
        scope: str,
        day_key: str,
        *,
        status_filter: set[str] | None = None,
        filled_only: bool = False,
        blocked_only: bool = False,
        limit: int = 500,
    ) -> list[dict]:
        filt: dict[str, Any] = {
            "tenant_id": tenant_id,
            "ledger_scope": scope,
            "day_key": day_key,
        }
        if filled_only:
            filt["status"] = "filled"
        elif blocked_only:
            filt["status"] = {"$in": sorted(BLOCKED_STATUSES)}
        elif status_filter is not None:
            filt["status"] = {"$in": sorted(status_filter)}
        cur = (
            self._db()[ORDERS_V2_COLLECTION]
            .find(filt)
            .sort([("ts_event", -1), ("display_seq", -1)])
            .limit(max(1, int(limit)))
        )
        return [public_order_view(d) for d in cur]

    def get_day_stats(self, tenant_id: str, scope: str, day_key: str) -> dict:
        doc = self._db()[DAY_STATS_COLLECTION].find_one(
            {"_id": compound_day_stats_id(tenant_id, scope, day_key)}
        )
        if doc:
            out = copy.deepcopy(doc)
            out.pop("_id", None)
            return out
        return self.rebuild_day_stats(tenant_id, scope, day_key)

    def rebuild_day_stats(self, tenant_id: str, scope: str, day_key: str) -> dict:
        from storage.mongo_client import assert_safe_dev_db_mutation, resolve_database_name

        assert_safe_dev_db_mutation(
            resolve_database_name(test=self._test, config=self._config),
            action="write",
        )
        filled = self.query_day(
            tenant_id, scope, day_key, filled_only=True, limit=10_000,
        )
        stats = empty_day_stats(tenant_id, scope, day_key)
        stats.update(stats_from_filled_orders(filled))
        blocked = self.query_day(
            tenant_id, scope, day_key, blocked_only=True, limit=10_000,
        )
        stats["blocked"] = len(blocked)
        by_st: dict[str, int] = {}
        for o in blocked:
            st = (o.get("status") or "").lower()
            by_st[st] = by_st.get(st, 0) + 1
        stats["blocked_by_status"] = by_st
        stats["updated_at"] = datetime.now().isoformat()
        payload = dict(stats)
        payload["_id"] = compound_day_stats_id(tenant_id, scope, day_key)
        self._db()[DAY_STATS_COLLECTION].replace_one(
            {"_id": payload["_id"]}, payload, upsert=True,
        )
        out = copy.deepcopy(stats)
        return out
