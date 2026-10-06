#!/usr/bin/env python3
"""Remove ledger and memory rows named by an external id list.

The id list is a runtime file (``--ids``). It is not shipped in the repo.
Dry-run is the default: it prints what would be removed, stored balances next
to a replay of the original fills, the sha256 of the id file, and the
per-store per-tenant delete counts in the ``expected_counts`` shape.
A real run requires ``--apply`` plus that same id-file sha256, the sha256 of
a fresh backup file, expected counts that match, every ledger tenant named
in the file, and a server that can run a multi-document transaction.

Lots, orders, and trade-history rows are removed by id. Embedded orders and
trades live in one document per tenant and scope; array indexes are only a
cross-check. Memory rows are selected by id, never by the tenant field.
``dca_policy`` rows that appeared after the list was built are the one
labelled exception: symbol and cutoff come from the id file.

Logs and Redis are not read or written.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

STORE_ORDERS_V2 = "mongo.orders_v2"
STORE_ORDERS = "mongo.orders (embedded entries)"
STORE_POSITIONS = "mongo.positions (lots)"
STORE_TRADES = "mongo.trade_history (embedded trades)"
STORE_MEMORY_TRADES = "mongo.memory_trades"
STORE_EVENTS = "mongo.memory_market_events"
STORE_RAG = "mongo.memory_rag_chunks"
STORE_LESSONS = "mongo.memory_lessons"
STORE_PROFILES = "mongo.memory_coin_profiles"

MEMORY_KINDS = ("trades", "events", "rag", "lessons", "profiles")
_KIND_STORE = {
    "trades": STORE_MEMORY_TRADES,
    "events": STORE_EVENTS,
    "rag": STORE_RAG,
    "lessons": STORE_LESSONS,
    "profiles": STORE_PROFILES,
}
_STORE_KIND = {store: kind for kind, store in _KIND_STORE.items()}

# Schema field names from the id-list format. They classify keep-buckets.
# They are not a list of tenants or coins to hardcode as targets.
_HENRY_KEEP_MARKERS = ("0812", "12_08", "12.08")
_CTEXP_KEEP_MARKER = "ctexp"

TOUCHED_STORES = frozenset(
    {
        STORE_ORDERS_V2,
        STORE_ORDERS,
        STORE_POSITIONS,
        STORE_TRADES,
        STORE_MEMORY_TRADES,
        STORE_EVENTS,
        STORE_RAG,
        STORE_LESSONS,
        STORE_PROFILES,
        "portfolio_nav_daily",
    }
)
UNTOUCHED = frozenset(
    {
        "redis",
        "logs",
        "aria_log",
        "decisions",
        "position_snapshots",
        "risk_rejects",
        "buy_decision_tape",
        "memory_path_stats",
        "memory_social_feed",
        "tenant_watchlists",
    }
)

# to_delete counts, per store and per tenant. Never a single total.
# Events and RAG may be null on a first dry-run; --apply still requires every one.
COUNT_STORES = (
    STORE_ORDERS_V2,
    STORE_ORDERS,
    STORE_POSITIONS,
    STORE_TRADES,
    STORE_MEMORY_TRADES,
    STORE_LESSONS,
    STORE_PROFILES,
    STORE_EVENTS,
    STORE_RAG,
)

_EXPECTED_COUNTS_HELP = """
expected_counts (inside the --ids JSON, bound to the file sha256)

Per store and per tenant. Each number is to_delete for that pair, not a
combined total. Tenant keys are the ledger tenant ids, plus any extra memory
count bucket the dry-run prints (a memory row is still deleted by id; the
bucket is only the tally). Use 0 when that tenant has nothing to delete.
JSON null, or a missing store, means unknown. That is allowed on a first
dry-run for events and RAG: the dry-run prints a block between
--- expected_counts --- and --- end expected_counts ---. That block is the
expected_counts object. Merge it into the id file, re-hash, and dry-run
again. --apply refuses unless every store below is present and every tenant
count is an integer that matches.

{
  "expected_counts": {
    "mongo.orders_v2": {"tenant_a": 2, "tenant_h": 1},
    "mongo.orders (embedded entries)": {"tenant_a": 2, "tenant_h": 1},
    "mongo.positions (lots)": {"tenant_a": 1, "tenant_h": 1},
    "mongo.trade_history (embedded trades)": {"tenant_a": 2, "tenant_h": 1},
    "mongo.memory_trades": {"tenant_a": 1, "tenant_h": 0},
    "mongo.memory_lessons": {"tenant_a": 0, "tenant_h": 1},
    "mongo.memory_coin_profiles": {"tenant_a": 1, "tenant_h": 0},
    "mongo.memory_market_events": null,
    "mongo.memory_rag_chunks": null
  }
}
"""


class CleanupRefused(Exception):
    """A real run was rejected before any read or write of the stores."""


class CleanupConflict(Exception):
    """A document changed between the plan read and the write."""


class CleanupAborted(Exception):
    """A write failed. Earlier transactions stay; this one stops."""

    def __init__(self, completed: list[str], label: str, cause: Exception):
        self.completed = list(completed)
        self.label = label
        self.cause = cause
        done = completed or ["nothing"]
        super().__init__(
            f"stopped on first error during {label}: {cause}; already committed: {done}"
        )


@dataclass
class WriteStep:
    name: str
    action: str
    doc: dict | None = None
    ids: list[str] | None = None
    kind: str | None = None
    tenant_id: str | None = None
    scope: str | None = None
    points: list | None = None
    baseline: Any = None
    baseline_id: str | None = None


@dataclass
class Plan:
    report: dict
    groups: list[tuple[str, list[WriteStep]]] = field(default_factory=list)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_dt(value: object) -> datetime | None:
    """Parse a timestamp and return naive UTC.

    Offsets other than Z are converted to UTC so they can be compared with
    the naive cutoff from the id file.
    """
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _round(value: float) -> float:
    return round(float(value), 8)


def _norm_symbol(value: object) -> str:
    return str(value or "").strip().upper().replace("-", "/")


def _position_symbol(key: str) -> str:
    from strategies.positions import parse_position_key

    symbol, _timeframe = parse_position_key(key)
    return _norm_symbol(symbol)


def parse_lot_id(lot_id: str) -> tuple[str, str, str] | None:
    """``positions:{tenant}:{scope}/{key}`` → tenant, scope, key."""
    prefix = "positions:"
    if not str(lot_id).startswith(prefix):
        return None
    rest = str(lot_id)[len(prefix) :]
    doc, sep, key = rest.partition("/")
    if not sep or not key or ":" not in doc:
        return None
    tenant, scope = doc.split(":", 1)
    if not tenant or not scope:
        return None
    return tenant, scope, key


def _tenant_from_doc_id(doc_id: object) -> str:
    text = str(doc_id or "")
    if ":" not in text:
        return ""
    return text.split(":", 1)[0]


def _doc_tenant(doc: dict) -> str:
    if doc.get("tenant_id"):
        return str(doc["tenant_id"])
    return _tenant_from_doc_id(doc.get("_id"))


def _doc_scope(doc: dict) -> str:
    if doc.get("ledger_scope"):
        return str(doc["ledger_scope"])
    text = str(doc.get("_id") or "")
    parts = text.split(":")
    if len(parts) >= 2:
        return parts[1]
    return ""


def _rows(spec: dict, store_name: str) -> list[dict]:
    body = (spec.get("stores") or {}).get(store_name) or {}
    rows = body.get("in_scope_ids") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _stringify_list(value: object) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def _flatten_ids(value: object) -> list[str]:
    if isinstance(value, dict):
        found: list[str] = []
        for inner in value.values():
            found.extend(_stringify_list(inner))
        return found
    return _stringify_list(value)


def _keep_bucket_name(key: str) -> str:
    lowered = key.lower()
    if _CTEXP_KEEP_MARKER in lowered:
        return "ctexp"
    if any(marker in lowered for marker in _HENRY_KEEP_MARKERS):
        return "henry_0812"
    return "other"


def collect_keep(spec: dict) -> dict[str, set[str]]:
    buckets = {"henry_0812": set(), "ctexp": set(), "other": set()}
    keep = spec.get("keep") or {}
    if isinstance(keep, dict):
        for key, value in keep.items():
            buckets[_keep_bucket_name(str(key))].update(_flatten_ids(value))
    stores = spec.get("stores") or {}
    if isinstance(stores, dict):
        for body in stores.values():
            if not isinstance(body, dict):
                continue
            for key, value in body.items():
                if not str(key).startswith("keep_"):
                    continue
                buckets[_keep_bucket_name(str(key))].update(_stringify_list(value))
    return buckets


def _kept_bucket(value: object, buckets: dict[str, set[str]]) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    candidates = {text}
    if ":" in text:
        candidates.add(text.split(":")[-1])
    if "#" in text:
        candidates.add(text.split("#")[-1])
    for name in ("henry_0812", "ctexp", "other"):
        keys = buckets[name]
        if candidates & keys:
            return name
        if any(text.endswith(":" + key) or text.endswith("#" + key) for key in keys):
            return name
    return None


def dca_symbols(spec: dict) -> set[str]:
    """Symbols for the labelled dca_policy rule, taken only from the id file."""
    found: set[str] = set()
    block = spec.get("dca_policy")
    if isinstance(block, dict):
        if block.get("symbol"):
            found.add(_norm_symbol(block["symbol"]))
        for item in block.get("symbols") or []:
            found.add(_norm_symbol(item))
    found.discard("")
    if found:
        return found
    positions = spec.get("positions") or {}
    if isinstance(positions, dict):
        for row in positions.values():
            if not isinstance(row, dict):
                continue
            parsed = parse_lot_id(str(row.get("lot_id") or ""))
            if parsed:
                found.add(_position_symbol(parsed[2]))
    for row in _rows(spec, STORE_POSITIONS):
        found.add(_position_symbol(str(row.get("key") or "")))
    for row in _rows(spec, STORE_PROFILES):
        tail = str(row.get("_id") or "").split("|")[-1]
        if "/" in tail:
            found.add(_norm_symbol(tail))
    found.discard("")
    return found


def file_ledger_tenants(spec: dict) -> set[str]:
    names: set[str] = set()
    positions = spec.get("positions") or {}
    if isinstance(positions, dict):
        for tenant in positions:
            text = str(tenant).strip()
            if text:
                names.add(text)
    for store_name in (STORE_ORDERS, STORE_ORDERS_V2, STORE_POSITIONS, STORE_TRADES):
        for row in _rows(spec, store_name):
            tenant = str(row.get("tenant") or "").strip() or _tenant_from_doc_id(row.get("doc_id"))
            if not tenant and store_name == STORE_ORDERS_V2:
                parts = str(row.get("_id") or "").split(":")
                if len(parts) >= 3:
                    tenant = parts[0]
            if tenant:
                names.add(tenant)
    return names


def _symbol_hit(values: Iterable[object], symbols: set[str]) -> bool:
    if not symbols:
        return False
    bases = {item.split("/")[0] for item in symbols}
    for raw in values:
        token = _norm_symbol(raw)
        if not token:
            continue
        if token in symbols or token in bases or token.split("/")[0] in bases:
            return True
    return False


def _is_dca_policy(doc: dict) -> bool:
    meta = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
    tokens = (
        doc.get("source"),
        doc.get("event_type"),
        meta.get("source"),
        meta.get("type"),
    )
    return any(str(token or "").strip().lower() == "dca_policy" for token in tokens)


def _event_symbols(doc: dict) -> list[str]:
    meta = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
    values = list(doc.get("symbols") or [])
    if meta.get("symbol"):
        values.append(meta.get("symbol"))
    if doc.get("symbol"):
        values.append(doc.get("symbol"))
    return [str(item) for item in values if item]


def _event_ts(doc: dict) -> datetime | None:
    meta = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
    for key in ("timestamp", "ts_utc", "ts"):
        parsed = _parse_dt(doc.get(key))
        if parsed is not None:
            return parsed
    return _parse_dt(meta.get("timestamp") or meta.get("ts_utc"))


def _order_ts(order: dict) -> datetime | None:
    stamps = order.get("timestamps") if isinstance(order.get("timestamps"), dict) else {}
    return _parse_dt(stamps.get("filled") or stamps.get("created") or order.get("timestamp"))


def _entry_order_id(entry: dict) -> str:
    return str(entry.get("id") or entry.get("order_id") or "").strip()


def _empty_counts() -> dict[str, int]:
    return {
        "matched": 0,
        "to_delete": 0,
        "kept_henry_0812": 0,
        "kept_ctexp": 0,
        "kept_before_cutoff": 0,
        "kept_other": 0,
    }


def _add_count(counts: dict[str, int], bucket: str) -> None:
    counts["matched"] += 1
    if bucket == "delete":
        counts["to_delete"] += 1
    elif bucket == "henry_0812":
        counts["kept_henry_0812"] += 1
    elif bucket == "ctexp":
        counts["kept_ctexp"] += 1
    elif bucket == "before_cutoff":
        counts["kept_before_cutoff"] += 1
    else:
        counts["kept_other"] += 1


def _store_row(store: str, tenant: str | None, counts: dict[str, int], *, selection: str) -> dict:
    row = {
        "store": store,
        "tenant": tenant,
        "selection": selection,
        "matched": counts["matched"],
        "to_delete": counts["to_delete"],
        "kept_henry_0812": counts["kept_henry_0812"],
        "kept_ctexp": counts["kept_ctexp"],
        "kept_before_cutoff": counts["kept_before_cutoff"],
        "kept_other": counts["kept_other"],
    }
    return row


def delete_order_ids(spec: dict, buckets: dict[str, set[str]]) -> dict[str, set[str]]:
    """Order ids to remove, per tenant. Keep-list ids are never included."""
    by_tenant: dict[str, set[str]] = {}

    def add(tenant: str, order_id: object) -> None:
        oid = str(order_id or "").strip()
        tid = str(tenant or "").strip()
        if not oid or not tid:
            return
        if _kept_bucket(oid, buckets):
            return
        by_tenant.setdefault(tid, set()).add(oid)

    positions = spec.get("positions") or {}
    if isinstance(positions, dict):
        for tenant, row in positions.items():
            if not isinstance(row, dict):
                continue
            add(str(tenant), row.get("entry_order"))
            for order_id in row.get("dca_orders") or []:
                add(str(tenant), order_id)
    for row in _rows(spec, STORE_ORDERS_V2) + _rows(spec, STORE_ORDERS) + _rows(spec, STORE_TRADES):
        tenant = str(row.get("tenant") or "").strip() or _tenant_from_doc_id(row.get("doc_id"))
        if not tenant and row.get("_id"):
            parts = str(row["_id"]).split(":")
            if len(parts) >= 3:
                tenant = parts[0]
        add(tenant, row.get("order_id"))
    return by_tenant


def position_targets(spec: dict) -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    def add(tenant: str, doc_id: str, key: str) -> None:
        item = (tenant, doc_id, key)
        if not tenant or not doc_id or not key or item in seen:
            return
        seen.add(item)
        found.append(item)

    positions = spec.get("positions") or {}
    if isinstance(positions, dict):
        for tenant, row in positions.items():
            if not isinstance(row, dict):
                continue
            parsed = parse_lot_id(str(row.get("lot_id") or ""))
            if parsed:
                parsed_tenant, scope, key = parsed
                add(parsed_tenant or str(tenant), f"{parsed_tenant}:{scope}", key)
    for row in _rows(spec, STORE_POSITIONS):
        doc_id = str(row.get("doc_id") or "")
        add(_tenant_from_doc_id(doc_id), doc_id, str(row.get("key") or ""))
    return found


def _listed_ids(spec: dict, store_name: str) -> set[str]:
    found: set[str] = set()
    for row in _rows(spec, store_name):
        if row.get("_id"):
            found.add(str(row["_id"]))
    return found


def _before_cutoff(ts: datetime | None, cutoff: datetime | None, symbols_hit: bool) -> bool:
    if not symbols_hit or ts is None or cutoff is None:
        return False
    return ts < cutoff


def _dca_rule_match(doc: dict, symbols: set[str], cutoff: datetime | None) -> bool:
    """Labelled rule: source dca_policy, symbol from the id file, ts >= cutoff.

    The document's tenant field is intentionally ignored.
    """
    if not symbols or cutoff is None:
        return False
    if not _is_dca_policy(doc):
        return False
    if not _symbol_hit(_event_symbols(doc), symbols):
        return False
    ts = _event_ts(doc)
    return ts is not None and ts >= cutoff


def _source_hits(source_id: object, anchors: set[str], buckets: dict[str, set[str]]) -> bool:
    source = str(source_id or "").strip()
    if not source:
        return False
    tail = source.split(":")[-1]
    if _kept_bucket(source, buckets) or _kept_bucket(tail, buckets):
        return False
    return source in anchors or tail in anchors


def _initial_capital(doc: dict | None) -> float | None:
    if not isinstance(doc, dict):
        return None
    for key in ("initial_capital", "initial_capital_usdt"):
        if doc.get(key) is not None:
            return _float(doc.get(key))
    return None


def _replay(orders: list[dict], initial: float) -> dict:
    from core.sim_ledger_replay import replay_simulated_ledger

    return replay_simulated_ledger(orders, initial)


def _order_day(order: dict) -> str:
    ts = _order_ts(order)
    if ts is None:
        return ""
    return ts.date().isoformat()


def _orders_through(orders: list[dict], day: str | None) -> list[dict]:
    if day is None:
        return list(orders)
    subset = [order for order in orders if _order_day(order) and _order_day(order) <= day]
    subset.extend(order for order in orders if not _order_day(order))
    return subset


def _replay_snapshot(orders: list[dict], initial: float, day: str | None) -> dict:
    """Replay cash, realized, and cost-basis mtm. Used only as a delta."""
    snap = _replay(_orders_through(orders, day), initial)
    mtm = 0.0
    for pos in (snap.get("positions") or {}).values():
        mtm += _float(pos.get("amount")) * _float(pos.get("average_entry"))
    return {
        "cash": _round(snap["cash"]),
        "realized": _round(snap["realized_pnl"]),
        "mtm": _round(mtm),
        "nav": _round(_float(snap["cash"]) + mtm),
        "open_positions": len(snap.get("positions") or {}),
    }


def _subtract_present(row: dict, key: str, delta: float) -> None:
    if key not in row or row[key] is None:
        return
    if not delta:
        return
    row[key] = _round(_float(row[key]) - delta)


def _adjust_nav_points(
    existing: list[dict],
    original: list[dict],
    remaining: list[dict],
    initial: float,
    cutoff: datetime,
) -> list[dict]:
    """Subtract the removed fills' per-day replay delta from stored NAV points.

    Points before the cutoff are copied unchanged. Points on or after the
    cutoff keep every stored field; only nav, cash, realized_pnl, and
    positions_mtm that are already present move by the replay delta. The
    series is not rebuilt at cost basis.
    """
    cutoff_day = cutoff.date().isoformat()
    adjusted: list[dict] = []
    for point in existing:
        row = copy.deepcopy(point)
        day = str(row.get("date") or "")[:10]
        if day and day >= cutoff_day:
            before = _replay_snapshot(original, initial, day)
            after = _replay_snapshot(remaining, initial, day)
            _subtract_present(row, "nav", before["nav"] - after["nav"])
            _subtract_present(row, "cash", before["cash"] - after["cash"])
            _subtract_present(row, "realized_pnl", before["realized"] - after["realized"])
            _subtract_present(row, "positions_mtm", before["mtm"] - after["mtm"])
        adjusted.append(row)
    return adjusted


def _order_owners(order_docs: list[dict]) -> dict[str, str]:
    found: dict[str, str] = {}
    ambiguous: set[str] = set()
    for doc in order_docs:
        tenant = _doc_tenant(doc)
        if not tenant:
            continue
        for entry in doc.get("orders") or []:
            if not isinstance(entry, dict):
                continue
            order_id = _entry_order_id(entry)
            if not order_id:
                continue
            previous = found.get(order_id)
            if previous is None:
                found[order_id] = tenant
            elif previous != tenant:
                ambiguous.add(order_id)
    for order_id in ambiguous:
        found.pop(order_id, None)
    return found


def _linked_order_tenant(blob: object, owners: dict[str, str]) -> str:
    text = str(blob or "").strip()
    if not text:
        return ""
    if text in owners:
        return owners[text]
    tail = text.split(":")[-1]
    if tail and tail in owners:
        return owners[tail]
    return ""


def memory_count_tenant(doc: dict, owners: dict[str, str], known_tenants: set[str]) -> str:
    """Tally bucket for a memory row. Deletion itself never consults this."""
    doc_id = str(doc.get("_id") or "")
    if "|" in doc_id:
        prefix = doc_id.split("|", 1)[0]
        if prefix in known_tenants:
            return prefix
    meta = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
    for blob in (doc_id, meta.get("source_id"), doc.get("source_id")):
        tenant = _linked_order_tenant(blob, owners)
        if tenant:
            return tenant
    label = str(doc.get("tenant_id") or doc.get("tenant") or "").strip()
    return label or "unattributed"


def commit_nav_result(ok: bool) -> None:
    if not ok:
        raise RuntimeError("nav history replace failed")


def actual_counts_block(counts: dict[tuple[str, str | None], dict[str, int]], tenants: list[str]) -> dict[str, dict[str, int]]:
    block: dict[str, dict[str, int]] = {store: {} for store in COUNT_STORES}
    for (store_name, tenant), row in counts.items():
        if store_name not in block or not tenant:
            continue
        deleted = int(row["to_delete"])
        if tenant not in tenants and deleted == 0:
            continue
        block[store_name][str(tenant)] = deleted
    for store_name in COUNT_STORES:
        for tenant in tenants:
            block[store_name].setdefault(tenant, 0)
        block[store_name] = {key: block[store_name][key] for key in sorted(block[store_name])}
    return block


def expected_count_problems(spec: dict, actual: dict[str, dict[str, int]], tenants: list[str]) -> list[str]:
    table = spec.get("expected_counts")
    if not isinstance(table, dict):
        return ["expected_counts object missing"]
    problems: list[str] = []
    for store_name in COUNT_STORES:
        body = table.get(store_name, None)
        found = actual.get(store_name) or {}
        if body is None or not isinstance(body, dict):
            problems.append(f"{store_name} missing or unknown")
            continue
        keys = set(tenants) | set(found) | {str(key) for key in body}
        for tenant in sorted(keys):
            if tenant not in body or body[tenant] is None:
                problems.append(f"{store_name} tenant {tenant} missing")
                continue
            try:
                expected = int(body[tenant])
            except (TypeError, ValueError):
                problems.append(f"{store_name} tenant {tenant} not an integer")
                continue
            actual_n = int(found.get(tenant, 0))
            if expected != actual_n:
                problems.append(
                    f"{store_name} tenant {tenant} expected {expected} actual {actual_n}"
                )
    return problems


def _index_notes(docs_by_id: dict[str, dict], rows: list[dict], *, entry_field: str) -> list[dict]:
    notes: list[dict] = []
    for row in rows:
        if row.get("index") is None:
            continue
        doc_id = str(row.get("doc_id") or "")
        expected = str(row.get("order_id") or "")
        try:
            index = int(row["index"])
        except (TypeError, ValueError):
            notes.append(
                {
                    "doc_id": doc_id,
                    "index": row.get("index"),
                    "expected_order_id": expected,
                    "found_order_id": None,
                    "status": "index_not_an_int",
                }
            )
            continue
        doc = docs_by_id.get(doc_id)
        if not isinstance(doc, dict):
            notes.append(
                {
                    "doc_id": doc_id,
                    "index": index,
                    "expected_order_id": expected,
                    "found_order_id": None,
                    "status": "doc_missing",
                }
            )
            continue
        entries = doc.get(entry_field) or []
        if not isinstance(entries, list) or index < 0 or index >= len(entries):
            status = "index_out_of_range"
            found = None
        else:
            entry = entries[index] if isinstance(entries[index], dict) else {}
            found = _entry_order_id(entry) or str(entry.get("id") or "")
            status = "match" if found == expected else "mismatch"
        notes.append(
            {
                "doc_id": doc_id,
                "index": index,
                "expected_order_id": expected,
                "found_order_id": found,
                "status": status,
            }
        )
    return notes


def _drop_order(entry: dict, delete_ids: set[str], buckets: dict[str, set[str]]) -> bool:
    order_id = _entry_order_id(entry)
    if not order_id or _kept_bucket(order_id, buckets):
        return False
    return order_id in delete_ids


def _trade_tokens(entry: dict) -> list[str]:
    tokens = []
    for key in ("order_id", "id", "trade_id"):
        text = str(entry.get(key) or "").strip()
        if text:
            tokens.append(text)
    return tokens


def _drop_trade(entry: dict, delete_ids: set[str], buckets: dict[str, set[str]]) -> bool:
    tokens = _trade_tokens(entry)
    if any(_kept_bucket(token, buckets) for token in tokens):
        return False
    return any(token in delete_ids for token in tokens)


class InMemoryCleanupStore:
    """Fixture store with the same document shapes the Mongo stores use."""

    def __init__(self) -> None:
        self.orders: dict[str, dict] = {}
        self.positions: dict[str, dict] = {}
        self.trades: dict[str, dict] = {}
        self.v2: dict[str, dict] = {}
        self.memory: dict[str, dict[str, dict]] = {kind: {} for kind in MEMORY_KINDS}
        self.nav: dict[tuple[str, str], list] = {}
        self.sentinels = {"logs": ["audit-trail"], "redis": {"cache": "warm"}}
        self.fail_on: tuple[str, str] | None = None
        self.nav_ok = True
        self.transactions_enabled = True
        self.committed_log: list[str] = []
        self._label = ""

    def order_docs(self) -> list[dict]:
        return [copy.deepcopy(doc) for doc in self.orders.values()]

    def position_docs(self) -> list[dict]:
        return [copy.deepcopy(doc) for doc in self.positions.values()]

    def trade_docs(self) -> list[dict]:
        return [copy.deepcopy(doc) for doc in self.trades.values()]

    def orders_v2_matching(self, keys: Iterable[str]) -> list[dict]:
        wanted = {str(key) for key in keys if key}
        found = []
        for doc_id, doc in self.v2.items():
            if doc_id in wanted or str(doc.get("id") or "") in wanted:
                found.append(copy.deepcopy(doc))
        return found

    def memory_docs(self, kind: str) -> list[dict]:
        return [copy.deepcopy(doc) for doc in self.memory[kind].values()]

    def nav_points(self, tenant_id: str, scope: str) -> list[dict]:
        return copy.deepcopy(self.nav.get((tenant_id, scope), []))

    def _snapshot(self) -> dict:
        return {
            "orders": copy.deepcopy(self.orders),
            "positions": copy.deepcopy(self.positions),
            "trades": copy.deepcopy(self.trades),
            "v2": copy.deepcopy(self.v2),
            "memory": copy.deepcopy(self.memory),
            "nav": copy.deepcopy(self.nav),
        }

    def _restore(self, snap: dict) -> None:
        self.orders = snap["orders"]
        self.positions = snap["positions"]
        self.trades = snap["trades"]
        self.v2 = snap["v2"]
        self.memory = snap["memory"]
        self.nav = snap["nav"]

    @contextmanager
    def transaction(self, label: str) -> Iterator[None]:
        self._label = label
        if not self.transactions_enabled:
            yield
            return
        snap = self._snapshot()
        try:
            yield
        except Exception:
            self._restore(snap)
            raise
        else:
            self.committed_log.append(label)

    def apply_step(self, step: WriteStep) -> None:
        if self.fail_on == (self._label, step.name):
            raise RuntimeError(f"injected failure {self._label}:{step.name}")
        if step.action == "replace_orders" and step.doc is not None:
            self.orders[str(step.doc["_id"])] = copy.deepcopy(step.doc)
        elif step.action == "replace_positions" and step.doc is not None:
            self.positions[str(step.doc["_id"])] = copy.deepcopy(step.doc)
        elif step.action == "replace_trades" and step.doc is not None:
            self.trades[str(step.doc["_id"])] = copy.deepcopy(step.doc)
        elif step.action == "delete_orders_v2":
            for doc_id in step.ids or []:
                self.v2.pop(str(doc_id), None)
        elif step.action == "delete_memory" and step.kind:
            bucket = self.memory[step.kind]
            for doc_id in step.ids or []:
                bucket.pop(str(doc_id), None)
        elif step.action == "replace_nav" and step.tenant_id and step.scope is not None:
            commit_nav_result(self.nav_ok)
            self.nav[(step.tenant_id, step.scope)] = copy.deepcopy(step.points or [])
        else:
            raise RuntimeError(f"unknown cleanup step {step.action}")
        if not self.transactions_enabled:
            self.committed_log.append(f"{self._label}:{step.name}")

    def to_obj(self) -> dict:
        nav = {f"{tenant}|{scope}": points for (tenant, scope), points in sorted(self.nav.items())}
        return {
            "orders": self.orders,
            "positions": self.positions,
            "trades": self.trades,
            "v2": self.v2,
            "memory": self.memory,
            "nav": nav,
            "sentinels": self.sentinels,
        }


class MongoCleanupStore:
    """Cleanup reads and writes through the ledger, v2, memory, and NAV stores."""

    def __init__(self) -> None:
        from intelligence.memory.rag_store import RagStore
        from intelligence.memory.store import MemoryStore
        from storage.mongo_ledger import MongoLedgerStore
        from storage.order_ledger_v2 import MongoOrderLedgerV2

        self.ledger = MongoLedgerStore()
        self.v2 = MongoOrderLedgerV2()
        self.memory = MemoryStore()
        self.rag = RagStore()
        self.transactions_enabled = self._probe_transactions()
        self.committed_log: list[str] = []
        self._label = ""
        self._session = None
        self._deferred_nav: list[tuple[str, str, list]] = []

    def _probe_transactions(self) -> bool:
        try:
            info = self.ledger._db.client.admin.command("hello")
        except Exception:
            return False
        if info.get("setName"):
            return True
        return str(info.get("msg") or "") == "isdbgrid"

    def _flush_nav_json(self) -> None:
        from services.portfolio_nav_history import replace_nav_points

        pending = self._deferred_nav
        self._deferred_nav = []
        for tenant_id, scope, points in pending:
            wrote = replace_nav_points(
                tenant_id,
                scope,
                points,
                write_json=True,
                write_mongo=False,
            )
            commit_nav_result(wrote)

    @contextmanager
    def transaction(self, label: str) -> Iterator[None]:
        self._label = label
        self._deferred_nav = []
        if not self.transactions_enabled:
            yield
            return
        client = self.ledger._db.client
        with client.start_session() as session:
            session.start_transaction()
            self._session = session
            try:
                yield
                session.commit_transaction()
            except Exception:
                session.abort_transaction()
                self._deferred_nav = []
                raise
            else:
                self.committed_log.append(label)
            finally:
                self._session = None
        self._flush_nav_json()

    def order_docs(self) -> list[dict]:
        from storage.mongo_ledger import ORDERS_COLLECTION

        return self.ledger.list_docs(ORDERS_COLLECTION, session=self._session)

    def position_docs(self) -> list[dict]:
        from storage.mongo_ledger import POSITIONS_COLLECTION

        return self.ledger.list_docs(POSITIONS_COLLECTION, session=self._session)

    def trade_docs(self) -> list[dict]:
        from storage.mongo_ledger import TRADE_HISTORY_COLLECTION

        return self.ledger.list_docs(TRADE_HISTORY_COLLECTION, session=self._session)

    def orders_v2_matching(self, keys: Iterable[str]) -> list[dict]:
        return self.v2.find_by_keys(keys, session=self._session)

    def memory_docs(self, kind: str) -> list[dict]:
        if kind == "rag":
            return self.rag.iter_docs(session=self._session)
        from intelligence.memory.store import (
            COL_EVENTS,
            COL_LESSONS,
            COL_PROFILES,
            COL_TRADES,
        )

        columns = {
            "trades": COL_TRADES,
            "events": COL_EVENTS,
            "lessons": COL_LESSONS,
            "profiles": COL_PROFILES,
        }
        return self.memory.iter_raw(columns[kind], session=self._session)

    def nav_points(self, tenant_id: str, scope: str) -> list[dict]:
        from services.portfolio_nav_history import load_nav_history

        return load_nav_history(tenant_id=tenant_id, scope=scope, session=self._session)

    def apply_step(self, step: WriteStep) -> None:
        from services.portfolio_nav_history import replace_nav_points
        from storage.mongo_ledger import (
            ORDERS_COLLECTION,
            POSITIONS_COLLECTION,
            TRADE_HISTORY_COLLECTION,
        )

        if step.action == "replace_orders" and step.doc is not None:
            self.ledger.replace_scoped_doc(ORDERS_COLLECTION, step.doc, session=self._session)
        elif step.action == "replace_positions" and step.doc is not None:
            self.ledger.replace_scoped_doc(POSITIONS_COLLECTION, step.doc, session=self._session)
        elif step.action == "replace_trades" and step.doc is not None:
            self.ledger.replace_scoped_doc(
                TRADE_HISTORY_COLLECTION, step.doc, session=self._session
            )
        elif step.action == "delete_orders_v2":
            self.v2.delete_by_ids(step.ids or [], session=self._session)
        elif step.action == "delete_memory" and step.kind:
            self._delete_memory(step.kind, step.ids or [])
        elif step.action == "replace_nav" and step.tenant_id and step.scope is not None:
            points = list(step.points or [])
            if self.transactions_enabled and self._session is not None:
                wrote = replace_nav_points(
                    step.tenant_id,
                    step.scope,
                    points,
                    session=self._session,
                    write_json=False,
                    write_mongo=True,
                )
                commit_nav_result(wrote)
                self._deferred_nav.append((step.tenant_id, step.scope, points))
            else:
                wrote = replace_nav_points(
                    step.tenant_id,
                    step.scope,
                    points,
                    write_json=True,
                    write_mongo=True,
                )
                commit_nav_result(wrote)
        else:
            raise RuntimeError(f"unknown cleanup step {step.action}")
        if not self.transactions_enabled:
            self.committed_log.append(f"{self._label}:{step.name}")

    def _delete_memory(self, kind: str, ids: list[str]) -> None:
        if kind == "rag":
            self.rag.delete_ids(ids, session=self._session)
            return
        from intelligence.memory.store import (
            COL_EVENTS,
            COL_LESSONS,
            COL_PROFILES,
            COL_TRADES,
        )

        columns = {
            "trades": COL_TRADES,
            "events": COL_EVENTS,
            "lessons": COL_LESSONS,
            "profiles": COL_PROFILES,
        }
        self.memory.delete_ids(columns[kind], ids, session=self._session)


def open_mongo_store() -> MongoCleanupStore:
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("PYTEST_RUNNING"):
        raise RuntimeError("refusing to open Mongo from the cleanup script during pytest")
    return MongoCleanupStore()


def build_plan(store: Any, spec: dict, tenants: list[str], *, ids_sha256: str, ids_file: str) -> Plan:
    selected = [tenant for tenant in tenants if tenant]
    selected_set = set(selected)
    buckets = collect_keep(spec)
    symbols = dca_symbols(spec)
    cutoff = _parse_dt(spec.get("cutoff_utc"))
    order_ids = delete_order_ids(spec, buckets)
    targets = position_targets(spec)
    memory_ok = (not file_ledger_tenants(spec)) or file_ledger_tenants(spec) <= selected_set

    order_docs = [doc for doc in store.order_docs() if isinstance(doc, dict)]
    position_docs = [doc for doc in store.position_docs() if isinstance(doc, dict)]
    trade_docs = [doc for doc in store.trade_docs() if isinstance(doc, dict)]
    orders_by_id = {str(doc.get("_id")): doc for doc in order_docs}
    trades_by_id = {str(doc.get("_id")): doc for doc in trade_docs}

    interest: set[str] = set()
    for ids in order_ids.values():
        interest.update(ids)
    for bucket_ids in buckets.values():
        interest.update(bucket_ids)
    for row in _rows(spec, STORE_ORDERS_V2):
        if row.get("_id"):
            interest.add(str(row["_id"]))
        if row.get("order_id"):
            interest.add(str(row["order_id"]))
    v2_docs = store.orders_v2_matching(interest) if interest else []

    warnings: list[str] = []
    if cutoff is None:
        warnings.append("id list has no cutoff_utc; dca_policy time rule and NAV window are off")
    if not memory_ok:
        missing = sorted(file_ledger_tenants(spec) - selected_set)
        warnings.append(
            "memory rows were not modified: pass every tenant named in the id list "
            f"({', '.join(missing)}). memory has no reliable tenant field and is selected by id only"
        )

    counts: dict[tuple[str, str | None], dict[str, int]] = {}

    def counts_for(store_name: str, tenant: str | None) -> dict[str, int]:
        key = (store_name, tenant)
        if key not in counts:
            counts[key] = _empty_counts()
        return counts[key]

    groups: dict[str, list[WriteStep]] = {tenant: [] for tenant in selected}
    preserved: list[dict] = []
    removed_orders_by_scope: dict[tuple[str, str], list[dict]] = {}
    original_orders_by_scope: dict[tuple[str, str], list[dict]] = {}
    scope_touched: set[tuple[str, str]] = set()

    for doc in order_docs:
        tenant = _doc_tenant(doc)
        scope = _doc_scope(doc)
        delete_ids = order_ids.get(tenant, set())
        entries = [entry for entry in (doc.get("orders") or []) if isinstance(entry, dict)]
        kept_entries = []
        removed = False
        for entry in entries:
            order_id = _entry_order_id(entry)
            # Index is never consulted. A keep-list id stays even if the file also lists it.
            if (
                tenant in selected_set
                and order_id in delete_ids
                and not _kept_bucket(order_id, buckets)
            ):
                removed = True
                continue
            kept_entries.append(entry)
        if tenant in selected_set:
            original_orders_by_scope[(tenant, scope)] = list(entries)
            removed_orders_by_scope[(tenant, scope)] = list(kept_entries)
            if removed:
                scope_touched.add((tenant, scope))
                updated = copy.deepcopy(doc)
                updated["orders"] = kept_entries
                groups[tenant].append(
                    WriteStep(
                        "orders",
                        "replace_orders",
                        doc=updated,
                        baseline=copy.deepcopy(doc),
                        baseline_id=str(doc.get("_id") or ""),
                    )
                )

    for doc in order_docs:
        tenant = _doc_tenant(doc)
        delete_ids = order_ids.get(tenant, set()) if tenant in selected_set else set()
        for entry in doc.get("orders") or []:
            if not isinstance(entry, dict):
                continue
            order_id = _entry_order_id(entry)
            keep = _kept_bucket(order_id, buckets)
            if keep:
                _add_count(counts_for(STORE_ORDERS, tenant), keep)
                continue
            if tenant in selected_set and order_id in delete_ids:
                _add_count(counts_for(STORE_ORDERS, tenant), "delete")
                continue
            if tenant in selected_set and _before_cutoff(
                _order_ts(entry),
                cutoff,
                _symbol_hit([entry.get("symbol")], symbols),
            ):
                _add_count(counts_for(STORE_ORDERS, tenant), "before_cutoff")

    for doc in v2_docs:
        if not isinstance(doc, dict):
            continue
        tenant = str(doc.get("tenant_id") or "") or _tenant_from_doc_id(doc.get("_id"))
        order_id = str(doc.get("id") or "")
        doc_id = str(doc.get("_id") or "")
        keep = _kept_bucket(order_id, buckets) or _kept_bucket(doc_id, buckets)
        if keep:
            _add_count(counts_for(STORE_ORDERS_V2, tenant), keep)
            continue
        if tenant in selected_set and (
            order_id in order_ids.get(tenant, set()) or doc_id in _listed_ids(spec, STORE_ORDERS_V2)
        ):
            _add_count(counts_for(STORE_ORDERS_V2, tenant), "delete")

    v2_ids_by_tenant: dict[str, list[str]] = {tenant: [] for tenant in selected}
    v2_baseline_by_tenant: dict[str, dict[str, dict]] = {tenant: {} for tenant in selected}
    for doc in v2_docs:
        if not isinstance(doc, dict):
            continue
        tenant = str(doc.get("tenant_id") or "") or _tenant_from_doc_id(doc.get("_id"))
        if tenant not in selected_set:
            continue
        order_id = str(doc.get("id") or "")
        doc_id = str(doc.get("_id") or "")
        if _kept_bucket(order_id, buckets) or _kept_bucket(doc_id, buckets):
            continue
        if order_id in order_ids.get(tenant, set()) or doc_id in _listed_ids(spec, STORE_ORDERS_V2):
            if doc_id:
                v2_ids_by_tenant.setdefault(tenant, []).append(doc_id)
                v2_baseline_by_tenant.setdefault(tenant, {})[doc_id] = copy.deepcopy(doc)
                scope_touched.add((tenant, str(doc.get("ledger_scope") or _doc_scope(doc))))
    for tenant, doc_ids in v2_ids_by_tenant.items():
        unique_ids = list(dict.fromkeys(doc_ids))
        if unique_ids:
            groups.setdefault(tenant, []).append(
                WriteStep(
                    "orders_v2",
                    "delete_orders_v2",
                    ids=unique_ids,
                    baseline=v2_baseline_by_tenant.get(tenant, {}),
                )
            )

    for doc in trade_docs:
        tenant = _doc_tenant(doc)
        scope = _doc_scope(doc)
        delete_ids = order_ids.get(tenant, set()) if tenant in selected_set else set()
        entries = [entry for entry in (doc.get("trades") or []) if isinstance(entry, dict)]
        kept_entries = []
        removed = False
        for entry in entries:
            tokens = _trade_tokens(entry)
            keep = next((bucket for token in tokens if (bucket := _kept_bucket(token, buckets))), None)
            if keep:
                _add_count(counts_for(STORE_TRADES, tenant), keep)
                kept_entries.append(entry)
                continue
            if tenant in selected_set and any(token in delete_ids for token in tokens):
                _add_count(counts_for(STORE_TRADES, tenant), "delete")
                removed = True
                continue
            if tenant in selected_set and _before_cutoff(
                _order_ts(entry) or _parse_dt(entry.get("timestamp")),
                cutoff,
                _symbol_hit([entry.get("symbol")], symbols),
            ):
                _add_count(counts_for(STORE_TRADES, tenant), "before_cutoff")
            kept_entries.append(entry)
        if removed and tenant in selected_set:
            scope_touched.add((tenant, scope))
            updated = copy.deepcopy(doc)
            updated["trades"] = kept_entries
            groups[tenant].append(
                WriteStep(
                    "trade_history",
                    "replace_trades",
                    doc=updated,
                    baseline=copy.deepcopy(doc),
                    baseline_id=str(doc.get("_id") or ""),
                )
            )

    keys_by_doc: dict[str, set[str]] = {}
    for tenant, doc_id, key in targets:
        if tenant in selected_set:
            keys_by_doc.setdefault(doc_id, set()).add(key)
    for doc in position_docs:
        tenant = _doc_tenant(doc)
        doc_id = str(doc.get("_id") or "")
        positions = doc.get("positions") if isinstance(doc.get("positions"), dict) else {}
        remove_keys = keys_by_doc.get(doc_id, set())
        if tenant not in selected_set:
            continue
        for key, lot in positions.items():
            keep = _kept_bucket(key, buckets)
            if keep:
                _add_count(counts_for(STORE_POSITIONS, tenant), keep)
            elif key in remove_keys:
                _add_count(counts_for(STORE_POSITIONS, tenant), "delete")
        if not remove_keys:
            continue
        updated_positions = {}
        changed = False
        for key, lot in positions.items():
            if key in remove_keys and not _kept_bucket(key, buckets):
                changed = True
                realized = _float((lot or {}).get("realized_pnl"), default=0.0) if isinstance(lot, dict) else 0.0
                preserved.append(
                    {
                        "tenant": tenant,
                        "doc_id": doc_id,
                        "key": key,
                        "realized_pnl": _round(realized),
                    }
                )
                continue
            updated_positions[key] = lot
        if changed:
            scope_touched.add((tenant, _doc_scope(doc)))
            updated = copy.deepcopy(doc)
            updated["positions"] = updated_positions
            groups[tenant].append(
                WriteStep(
                    "positions",
                    "replace_positions",
                    doc=updated,
                    baseline=copy.deepcopy(doc),
                    baseline_id=doc_id,
                )
            )

    metrics: list[dict] = []
    trade_by_scope = {(_doc_tenant(doc), _doc_scope(doc)): doc for doc in trade_docs}
    seen_scopes = set(original_orders_by_scope)
    for doc in trade_docs:
        tenant = _doc_tenant(doc)
        if tenant in selected_set:
            seen_scopes.add((tenant, _doc_scope(doc)))
    for tenant, scope in sorted(seen_scopes):
        original = original_orders_by_scope.get((tenant, scope), [])
        remaining = removed_orders_by_scope.get((tenant, scope), original)
        trade_doc = trade_by_scope.get((tenant, scope))
        initial = _initial_capital(trade_doc)
        if initial is None:
            initial = _initial_capital(
                next(
                    (
                        doc
                        for doc in order_docs
                        if _doc_tenant(doc) == tenant and _doc_scope(doc) == scope
                    ),
                    None,
                )
            )
        if initial is None:
            initial = 0.0
            warnings.append(f"missing initial_capital for {tenant}/{scope}; replay baseline is 0")
        before = _replay_snapshot(original, initial, None)
        after = _replay_snapshot(remaining, initial, None)
        cash_delta = _round(before["cash"] - after["cash"])
        realized_delta = _round(before["realized"] - after["realized"])
        open_delta = before["open_positions"] - after["open_positions"]
        touched = (tenant, scope) in scope_touched
        stored_cash = trade_doc.get("virtual_balance") if isinstance(trade_doc, dict) else None
        stored_realized = trade_doc.get("realized_pnl") if isinstance(trade_doc, dict) else None
        writes_metrics = False
        writes_nav = False
        if touched and isinstance(trade_doc, dict):
            prior = [
                step
                for step in groups[tenant]
                if step.action == "replace_trades"
                and step.doc
                and str(step.doc.get("_id")) == str(trade_doc.get("_id"))
            ]
            updated = copy.deepcopy(prior[0].doc if prior else trade_doc)
            if "virtual_balance" in trade_doc:
                _subtract_present(updated, "virtual_balance", cash_delta)
            if "realized_pnl" in trade_doc:
                _subtract_present(updated, "realized_pnl", realized_delta)
            if "open_positions" in trade_doc:
                _subtract_present(updated, "open_positions", open_delta)
            if prior:
                prior[0].doc = updated
                writes_metrics = True
            elif _stable(updated) != _stable(trade_doc):
                groups[tenant].append(
                    WriteStep(
                        "trade_history",
                        "replace_trades",
                        doc=updated,
                        baseline=copy.deepcopy(trade_doc),
                        baseline_id=str(trade_doc.get("_id") or ""),
                    )
                )
                writes_metrics = True
        existing_nav = store.nav_points(tenant, scope) if touched else []
        if touched and existing_nav and cutoff is not None:
            adjusted_nav = _adjust_nav_points(existing_nav, original, remaining, initial, cutoff)
            if _stable(adjusted_nav) != _stable(existing_nav):
                groups[tenant].append(
                    WriteStep(
                        "nav",
                        "replace_nav",
                        tenant_id=tenant,
                        scope=scope,
                        points=adjusted_nav,
                        baseline=copy.deepcopy(existing_nav),
                    )
                )
                writes_nav = True

        def _stored_after(stored: object, delta: float, write: bool) -> float | None:
            if stored is None:
                return None
            base = _round(_float(stored))
            if not write:
                return base
            return _round(base - delta)

        metrics.append(
            {
                "tenant": tenant,
                "scope": scope,
                "initial_capital": _round(initial),
                "stored_cash": None if stored_cash is None else _round(_float(stored_cash)),
                "replay_cash": before["cash"],
                "stored_realized": None if stored_realized is None else _round(_float(stored_realized)),
                "replay_realized": before["realized"],
                "cash_delta": cash_delta,
                "realized_delta": realized_delta,
                "cash_after": _stored_after(stored_cash, cash_delta, touched),
                "realized_after": _stored_after(stored_realized, realized_delta, touched),
                "writes_metrics": writes_metrics,
                "writes_nav": writes_nav,
            }
        )

    memory_steps: list[WriteStep] = []
    deleted_anchors: set[str] = set()
    for tenant in selected:
        deleted_anchors.update(order_ids.get(tenant, set()))
    owners = _order_owners(order_docs)
    known_tenants = file_ledger_tenants(spec) | selected_set

    def _tally_memory(kind: str, doc: dict, bucket_name: str) -> None:
        tenant_bucket = memory_count_tenant(doc, owners, known_tenants)
        _add_count(counts_for(_KIND_STORE[kind], tenant_bucket), bucket_name)

    memory_plan: dict[str, list[str]] = {kind: [] for kind in MEMORY_KINDS}
    memory_baseline: dict[str, dict[str, dict]] = {kind: {} for kind in MEMORY_KINDS}
    if memory_ok:
        for kind in ("trades", "lessons", "profiles"):
            listed = _listed_ids(spec, _KIND_STORE[kind])
            for doc in store.memory_docs(kind):
                doc_id = str(doc.get("_id") or "")
                keep = _kept_bucket(doc_id, buckets)
                if keep:
                    _tally_memory(kind, doc, keep)
                    continue
                if doc_id and doc_id in listed:
                    _tally_memory(kind, doc, "delete")
                    memory_plan[kind].append(doc_id)
                    memory_baseline[kind][doc_id] = copy.deepcopy(doc)
                    deleted_anchors.add(doc_id)
        listed_events = _listed_ids(spec, STORE_EVENTS)
        for doc in store.memory_docs("events"):
            doc_id = str(doc.get("_id") or doc.get("event_id") or "")
            keep = _kept_bucket(doc_id, buckets)
            if keep:
                _tally_memory("events", doc, keep)
                continue
            if (doc_id and doc_id in listed_events) or _dca_rule_match(doc, symbols, cutoff):
                _tally_memory("events", doc, "delete")
                if doc_id:
                    memory_plan["events"].append(doc_id)
                    memory_baseline["events"][doc_id] = copy.deepcopy(doc)
                    deleted_anchors.add(doc_id)
                continue
            if _is_dca_policy(doc) and _symbol_hit(_event_symbols(doc), symbols) and _before_cutoff(
                _event_ts(doc), cutoff, True
            ):
                _tally_memory("events", doc, "before_cutoff")
        listed_rag = _listed_ids(spec, STORE_RAG)
        for doc in store.memory_docs("rag"):
            doc_id = str(doc.get("_id") or doc.get("chunk_id") or "")
            meta = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
            source_id = meta.get("source_id") or doc.get("source_id")
            keep = _kept_bucket(doc_id, buckets) or _kept_bucket(source_id, buckets)
            if keep:
                _tally_memory("rag", doc, keep)
                continue
            if (doc_id and doc_id in listed_rag) or _source_hits(source_id, deleted_anchors, buckets):
                _tally_memory("rag", doc, "delete")
                if doc_id:
                    memory_plan["rag"].append(doc_id)
                    memory_baseline["rag"][doc_id] = copy.deepcopy(doc)
        for kind, step_name in (
            ("trades", "memory_trades"),
            ("events", "memory_events"),
            ("rag", "memory_rag"),
            ("lessons", "memory_lessons"),
            ("profiles", "memory_profiles"),
        ):
            unique_ids = list(dict.fromkeys(memory_plan[kind]))
            if unique_ids:
                memory_steps.append(
                    WriteStep(
                        step_name,
                        "delete_memory",
                        kind=kind,
                        ids=unique_ids,
                        baseline={doc_id: memory_baseline[kind][doc_id] for doc_id in unique_ids if doc_id in memory_baseline[kind]},
                    )
                )

    for tenant in selected:
        for store_name in COUNT_STORES:
            counts_for(store_name, tenant)

    store_rows = []
    for store_name, tenant in sorted(counts, key=lambda item: (item[0], item[1] or "")):
        selection = "by_id"
        if store_name == STORE_EVENTS:
            selection = "by_id_plus_dca_policy_symbol_ts"
        elif store_name == STORE_RAG:
            selection = "by_id_plus_source_id"
        elif store_name in {STORE_ORDERS, STORE_TRADES}:
            selection = "by_order_id"
        store_rows.append(_store_row(store_name, tenant, counts[(store_name, tenant)], selection=selection))

    index_notes = _index_notes(orders_by_id, _rows(spec, STORE_ORDERS), entry_field="orders")
    index_notes.extend(_index_notes(trades_by_id, _rows(spec, STORE_TRADES), entry_field="trades"))

    ordered_groups = [(tenant, groups.get(tenant, [])) for tenant in selected]
    if memory_steps:
        ordered_groups.append(("memory", memory_steps))

    counts_block = actual_counts_block(counts, selected)
    count_problems = expected_count_problems(spec, counts_block, selected)
    if count_problems:
        warnings.append(
            "expected_counts do not match this plan (paste the expected_counts block, "
            "re-hash, and dry-run again): " + "; ".join(count_problems)
        )

    report = {
        "mode": "dry-run",
        "ids_file": ids_file,
        "ids_sha256": ids_sha256,
        "tenants": selected,
        "cutoff_utc": spec.get("cutoff_utc"),
        "dca_policy_symbols": sorted(symbols),
        "dca_policy_rule": "source=dca_policy AND symbol from the id file AND ts >= cutoff_utc",
        "logs": "untouched",
        "redis": "untouched",
        "memory_selected_by": "id",
        "memory_applied": memory_ok,
        "expected_counts_actual": counts_block,
        "expected_count_problems": count_problems,
        "stores": store_rows,
        "index_cross_check": index_notes,
        "metrics": metrics,
        "preserved_lot_realized_pnl": preserved,
        "warnings": warnings,
    }
    return Plan(report=report, groups=ordered_groups)


def _stable(value: object) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _find_by_id(docs: list[dict], doc_id: object) -> dict | None:
    for doc in docs:
        if isinstance(doc, dict) and str(doc.get("_id")) == str(doc_id):
            return doc
    return None


def _live_baseline(store: Any, step: WriteStep) -> object:
    if step.action == "replace_orders":
        return _find_by_id(store.order_docs(), step.baseline_id)
    if step.action == "replace_positions":
        return _find_by_id(store.position_docs(), step.baseline_id)
    if step.action == "replace_trades":
        return _find_by_id(store.trade_docs(), step.baseline_id)
    if step.action == "delete_orders_v2":
        docs = store.orders_v2_matching(step.ids or [])
        return {str(doc.get("_id")): doc for doc in docs if isinstance(doc, dict)}
    if step.action == "delete_memory":
        wanted = {str(item) for item in (step.ids or [])}
        docs = store.memory_docs(step.kind or "")
        return {
            str(doc.get("_id")): doc
            for doc in docs
            if isinstance(doc, dict) and str(doc.get("_id")) in wanted
        }
    if step.action == "replace_nav":
        return store.nav_points(step.tenant_id or "", step.scope or "")
    raise RuntimeError(f"unknown cleanup step {step.action}")


def apply_plan(store: Any, plan: Plan) -> list[str]:
    completed: list[str] = []
    for label, steps in plan.groups:
        if not steps:
            continue
        try:
            with store.transaction(label):
                for step in steps:
                    live = _live_baseline(store, step)
                    if _stable(live) != _stable(step.baseline):
                        raise CleanupConflict(
                            f"abort: {step.name} changed since the plan snapshot; refusing to write"
                        )
                    store.apply_step(step)
        except Exception as exc:
            done = list(getattr(store, "committed_log", completed))
            raise CleanupAborted(done, label, exc) from exc
        completed.append(label)
    return completed


def format_report(report: dict) -> str:
    lines = [
        f"mode: {report.get('mode')}",
        f"ids_sha256: {report.get('ids_sha256')}",
        f"tenants: {','.join(report.get('tenants') or [])}",
        "logs: untouched",
        "redis: untouched",
    ]
    for row in report.get("stores") or []:
        lines.append(
            "store={store} tenant={tenant} matched={matched} to_delete={to_delete} "
            "kept_henry_0812={kept_henry_0812} kept_ctexp={kept_ctexp} "
            "kept_before_cutoff={kept_before_cutoff}".format(**row)
        )
    for row in report.get("preserved_lot_realized_pnl") or []:
        lines.append(
            "lot_realized_pnl tenant={tenant} doc_id={doc_id} key={key} value={realized_pnl}".format(**row)
        )
    for row in report.get("metrics") or []:
        lines.append(
            "metrics tenant={tenant} scope={scope} stored_cash={stored_cash} replay_cash={replay_cash} "
            "stored_realized={stored_realized} replay_realized={replay_realized} "
            "cash_delta={cash_delta} realized_delta={realized_delta} "
            "cash_after={cash_after} realized_after={realized_after} "
            "write_metrics={writes_metrics} write_nav={writes_nav}".format(**row)
        )
    lines.append("--- expected_counts ---")
    lines.append(
        json.dumps({"expected_counts": report.get("expected_counts_actual") or {}}, indent=2)
    )
    lines.append("--- end expected_counts ---")
    for note in report.get("warnings") or []:
        lines.append(f"warning: {note}")
    lines.append("--- json ---")
    lines.append(json.dumps(report, indent=2, sort_keys=True))
    return "\n".join(lines)


def _apply_refusal(args: argparse.Namespace, ids_digest: str) -> str | None:
    if not args.backup or not args.backup_sha256 or not args.ids_sha256:
        return (
            "refusing real run: pass --backup, --backup-sha256, and --ids-sha256 "
            "(dry-run is the default)"
        )
    if args.ids_sha256.strip().lower() != ids_digest:
        return "refusing real run: --ids-sha256 does not match the --ids file"
    backup = Path(args.backup)
    if not backup.is_file():
        return "refusing real run: backup file not found"
    if sha256_file(backup) != args.backup_sha256.strip().lower():
        return "refusing real run: --backup-sha256 does not match the backup file"
    return None


def execute(argv: list[str] | None = None, *, store: Any = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dry-run or apply an id-list cleanup. Does not rewrite logs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_EXPECTED_COUNTS_HELP,
    )
    parser.add_argument("--ids", required=True, help="Path to the id-list JSON. Not stored in the repo.")
    parser.add_argument(
        "--tenant",
        action="append",
        required=True,
        dest="tenants",
        help="Tenant id to edit. Repeat for each tenant. No default.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the cleanup. Requires backup and id-file sha256. Default is dry-run.",
    )
    parser.add_argument("--backup", default=None, help="Fresh backup file whose bytes are hashed.")
    parser.add_argument("--backup-sha256", default=None, help="Expected sha256 of --backup.")
    parser.add_argument(
        "--ids-sha256",
        default=None,
        help="sha256 of --ids printed by the dry-run. Required for --apply.",
    )
    args = parser.parse_args(argv)
    ids_path = Path(args.ids)
    if not ids_path.is_file():
        print(f"refusing: --ids file not found: {ids_path}")
        return 2
    ids_digest = sha256_file(ids_path)
    if args.ids_sha256 and args.ids_sha256.strip().lower() != ids_digest:
        print("refusing: --ids-sha256 does not match the --ids file")
        print(f"ids_sha256: {ids_digest}")
        return 2
    if args.apply:
        refusal = _apply_refusal(args, ids_digest)
        if refusal:
            print(refusal)
            print(f"ids_sha256: {ids_digest}")
            return 2
    try:
        spec = json.loads(ids_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"refusing: --ids is not JSON: {exc}")
        return 2
    if not isinstance(spec, dict):
        print("refusing: --ids JSON must be an object")
        return 2
    if store is None:
        try:
            store = open_mongo_store()
        except RuntimeError as exc:
            print(str(exc))
            return 2
    tenants = []
    seen: set[str] = set()
    for tenant in args.tenants:
        text = str(tenant).strip()
        if text and text not in seen:
            seen.add(text)
            tenants.append(text)
    plan = build_plan(
        store,
        spec,
        tenants,
        ids_sha256=ids_digest,
        ids_file=str(ids_path),
    )
    if args.apply:
        missing_tenants = sorted(file_ledger_tenants(spec) - set(tenants))
        if missing_tenants:
            print(
                "refusing real run: --tenant must name every ledger tenant in the id file "
                f"({', '.join(missing_tenants)}). memory has no reliable tenant field "
                "and is selected by id only"
            )
            print(format_report(plan.report))
            return 2
        if not getattr(store, "transactions_enabled", False):
            print("refusing real run: multi-document transactions are not available")
            print(format_report(plan.report))
            return 2
        count_problems = list(plan.report.get("expected_count_problems") or [])
        if count_problems:
            print("refusing real run: expected_counts do not match the plan")
            for item in count_problems:
                print(f"count: {item}")
            print(format_report(plan.report))
            return 2
        try:
            apply_plan(store, plan)
        except CleanupAborted as exc:
            print(str(exc))
            print(format_report(plan.report))
            return 1
        plan.report["mode"] = "applied"
    print(format_report(plan.report))
    return 0


def main() -> int:
    return execute()


if __name__ == "__main__":
    raise SystemExit(main())
