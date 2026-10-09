#!/usr/bin/env python3
"""Backfill filled blob orders into orders_v2, then prove blob ↔ v2 parity.

Replaces the #123 upsert-everything tool. Default is a dry run. Writing
requires ``--apply``. ``--verify`` is the read-only parity check (R2).

``ORDER_LEDGER_V2_BACKFILL_COMPLETE`` stays 0 until ``--verify`` reports
every count at 0. This script does not set that flag and does not tell
the operator to set it.

The operator entry point refuses every database other than ``xagent_test``
on the env-test Mongo, with ``ORDER_LEDGER_V2_BACKEND=mongo`` set
explicitly, and it does that before any ledger read. A real ``--apply``
against ``xagent_test`` is a data write and needs its own yes; this
module does not run one.

No coin names, no connection strings, no hosts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Iterable

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

ARCHIVE_PREFIX = "orders_v2_archive_"
ARCHIVE_FIELDS = ("archived_at", "reason", "blob_order_id")
ARCHIVE_REASON = "cause-b-orphan"
AUDIT_COLLECTION = "ledger_manual_reverts"
ORDERS_COLLECTION = "orders"
ORDERS_V2_COLLECTION = "orders_v2"
DAY_STATS_COLLECTION = "order_day_stats"
POSITIONS_COLLECTION = "positions"
TRADE_HISTORY_COLLECTION = "trade_history"
JULY_DAYS = (
    "2026-07-24",
    "2026-07-25",
    "2026-07-26",
    "2026-07-27",
    "2026-07-28",
)
OPERATOR_DB = "xagent_test"
PROD_DB = "xagent"
DUMPER = "raw-bson-sha256-v1"
_CENT = Decimal("0.01")


class _Abort(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class ApplyHooks:
    """Test-only fault points. The CLI never sets these."""

    fail_archive_readback: set[str] = field(default_factory=set)
    mutate_v2_before_delete: bool = False
    fail_upsert_ids: set[str] = field(default_factory=set)
    fail_readback_ids: set[str] = field(default_factory=set)
    store_none: bool = False
    degraded: bool = False
    abort_after_delete: bool = False
    deleted_count_override: int | None = None
    after_archive: Callable[[], None] | None = None
    before_insert: Callable[[], None] | None = None
    after_writes: Callable[[], None] | None = None


@dataclass
class JobResult:
    exit_code: int
    lines: list[str]
    found: dict | None = None
    verify: dict | None = None


def script_path() -> Path:
    return Path(__file__).resolve()


def script_sha256() -> str:
    return sha256_bytes(script_path().read_bytes())


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def ledger_collection_names(run_id: str | None) -> list[str]:
    names = [
        ORDERS_COLLECTION,
        ORDERS_V2_COLLECTION,
        AUDIT_COLLECTION,
        DAY_STATS_COLLECTION,
        POSITIONS_COLLECTION,
        TRADE_HISTORY_COLLECTION,
    ]
    if run_id:
        names.insert(2, archive_name(run_id))
    return names


def archive_name(run_id: str) -> str:
    if not run_id or any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_" for ch in run_id):
        raise _Abort("run id must be letters, digits, and underscore only")
    return f"{ARCHIVE_PREFIX}{run_id}"


def raw_bson_sha256(db, name: str) -> str:
    """Hash a collection the way the #648 backup hashed raw BSON.

    Documents are encoded in ``_id`` order. A missing collection hashes as
    empty input so an archive that does not exist yet has a stable digest.
    """
    from bson import encode

    digest = hashlib.sha256()
    if name not in set(db.list_collection_names()):
        return digest.hexdigest()
    for doc in db[name].find().sort("_id", 1):
        digest.update(encode(doc))
    return digest.hexdigest()


def hash_ledger(db, run_id: str | None) -> dict[str, str]:
    return {name: raw_bson_sha256(db, name) for name in ledger_collection_names(run_id)}


def write_backup_manifest(db, path: str | Path, run_id: str | None) -> str:
    payload = {"dumper": DUMPER, "collections": hash_ledger(db, run_id)}
    raw = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    Path(path).write_bytes(raw)
    return sha256_bytes(raw)


def load_backup_manifest(path: str | Path, expected_sha: str) -> dict[str, str]:
    file_path = Path(path)
    if not file_path.is_file():
        raise _Abort("backup hash file is missing")
    raw = file_path.read_bytes()
    if sha256_bytes(raw) != (expected_sha or "").strip().lower():
        raise _Abort("backup sha256 does not match the backup file")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise _Abort("backup hash file is not json") from exc
    if payload.get("dumper") != DUMPER:
        raise _Abort("backup hash file was not made by the raw-bson dumper")
    cols = payload.get("collections")
    if not isinstance(cols, dict) or not cols:
        raise _Abort("backup hash file has no collection hashes")
    return {str(k): str(v) for k, v in cols.items()}


def host_is_env_test_mongo(host: str) -> bool:
    """True only for the env-test Mongo. Production never passes.

    The hostname is not stored in this repo. Either the process is in
    Railway environment ``test``, or ``XAGENT_ENV_TEST_MONGO_HOST`` is set
    to that host and matches. A production stack or environment fails.
    """
    env = (os.environ.get("RAILWAY_ENVIRONMENT") or "").strip().lower()
    stack = (os.environ.get("BOT_STACK") or "").strip().lower()
    if env in {"production", "prod"} or stack in {"production", "prod", "live"}:
        return False
    cleaned = (host or "").strip()
    if not cleaned or cleaned.lower() == "unknown":
        return False
    allowed = (os.environ.get("XAGENT_ENV_TEST_MONGO_HOST") or "").strip()
    if allowed:
        return cleaned == allowed
    return env == "test"


def operator_gate() -> tuple[dict | None, str | None]:
    """C1. Returns ``(summary, error)``. ``error`` set means STOP.

    ``prepare_operator_mongo`` is not called until the database name and
    the v2 backend already look like the operator target. Calling it
    earlier would default an unset ``MONGODB_DB`` to ``xagent_test`` and
    hide the production fallback.
    """
    from storage.mongo_client import resolve_database_name

    resolved = resolve_database_name()
    pinned = os.environ.get("MONGODB_DB")
    if pinned != OPERATOR_DB or resolved != OPERATOR_DB:
        shown = pinned or resolved or PROD_DB
        return {"db": shown, "host": "", "pytest_isolated": False}, (
            f"C1: db is not {OPERATOR_DB} (resolved {shown})"
        )
    backend = (os.environ.get("ORDER_LEDGER_V2_BACKEND") or "").strip().lower()
    if backend != "mongo":
        return {"db": resolved, "host": "", "pytest_isolated": False}, (
            "C1: v2 store is not Mongo-backed"
        )
    from scripts.operator_mongo import prepare_operator_mongo

    summary = prepare_operator_mongo()
    if summary.get("db") != OPERATOR_DB:
        return summary, f"C1: db is not {OPERATOR_DB}"
    if not host_is_env_test_mongo(str(summary.get("host") or "")):
        return summary, "C1: host is not the env-test Mongo"
    return summary, None


def format_summary(summary: dict | None) -> str:
    summary = summary or {}
    host = str(summary.get("host") or "")
    if "://" in host or "@" in host or "/" in host:
        host = "redacted"
    return (
        f"db={summary.get('db')} host={host} "
        f"pytest_isolated={summary.get('pytest_isolated')}"
    )


def _money(value: object) -> Decimal:
    try:
        return Decimal(str(value if value is not None else "0")).quantize(_CENT, rounding=ROUND_HALF_UP)
    except Exception:
        return Decimal("0.00")


def _usdt(order: dict) -> Decimal:
    from core.sim_ledger_replay import _filled_order_usdt

    return _money(_filled_order_usdt(order))


def _filled(order: dict) -> bool:
    from core.models import is_executed_status

    return is_executed_status(order.get("status"))


def _parse_time(value: object) -> datetime | None:
    if not value:
        return None
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _seq(order: dict) -> int:
    try:
        return int(order.get("display_seq"))
    except (TypeError, ValueError):
        return 0


def _oid(order: dict) -> str:
    return str(order.get("id") or "")


def _side(order: dict) -> str:
    return str(order.get("side") or "").strip().lower()


def _field(order: dict, key: str) -> object:
    execution = order.get("execution") or {}
    request = order.get("request") or {}
    if key in execution and execution.get(key) is not None:
        return execution.get(key)
    if key in request and request.get(key) is not None:
        return request.get(key)
    return order.get(key)


def _fill_stamp(order: dict) -> str:
    ts = order.get("timestamps") or {}
    return str(ts.get("filled") or "")


def blob_orders(db, tenant: str, scope: str) -> list[dict]:
    coll = db[ORDERS_COLLECTION]
    keys = [
        {"tenant_id": tenant, "ledger_scope": scope},
        {"_id": f"{tenant}:{scope}"},
    ]
    if tenant == "default":
        keys.append({"_id": scope})
    seen: set[str] = set()
    docs = []
    for query in keys:
        for doc in coll.find(query):
            marker = str(doc.get("_id"))
            if marker in seen:
                continue
            seen.add(marker)
            docs.append(doc)
    orders: list[dict] = []
    known: set[str] = set()
    for doc in docs:
        for order in doc.get("orders") or []:
            if not isinstance(order, dict):
                continue
            tid = str(order.get("tenant_id") or doc.get("tenant_id") or tenant)
            sc = str(order.get("ledger_scope") or doc.get("ledger_scope") or scope)
            if tid != tenant or sc != scope:
                continue
            oid = _oid(order)
            if not oid or oid in known:
                continue
            known.add(oid)
            orders.append(order)
    return orders


def v2_orders(db, tenant: str, scope: str) -> list[dict]:
    return list(
        db[ORDERS_V2_COLLECTION].find({"tenant_id": tenant, "ledger_scope": scope})
    )


def _max_seq(orders: Iterable[dict]) -> int:
    seqs = [_seq(o) for o in orders]
    return max(seqs) if seqs else 0


def _dry_run_fill(order: dict) -> bool:
    status = str(order.get("status") or "").strip().lower()
    if status not in {"cancelled", "canceled"}:
        return False
    execution = order.get("execution") or {}
    return isinstance(execution, dict) and bool(execution)


def survey(db, tenants: list[str], scope: str, as_of: str | None) -> dict:
    """Read-only picture of the gap. Keyed by order id, never display_seq."""
    cutoff = _parse_time(as_of) if as_of else None
    block: dict[str, Any] = {"as_of": as_of, "scope": scope, "tenants": {}}
    for tenant in tenants:
        blob = blob_orders(db, tenant, scope)
        v2 = v2_orders(db, tenant, scope)
        blob_ids = {_oid(o) for o in blob}
        v2_by_id = {_oid(d): d for d in v2 if _oid(d)}
        v2_by_seq = {_seq(d): d for d in v2}
        orphans = [d for d in v2 if _oid(d) and _oid(d) not in blob_ids]
        filled_orphans = [d for d in orphans if _filled(d)]
        inserts = []
        for order in blob:
            if not _filled(order):
                continue
            if _oid(order) in v2_by_id:
                continue
            if cutoff is not None:
                stamp = _parse_time(_fill_stamp(order))
                if stamp is None or stamp > cutoff:
                    continue
            inserts.append(order)
        insert_now = []
        blocked = []
        held_by_blob = []
        for order in inserts:
            holder = v2_by_seq.get(_seq(order))
            if holder is None:
                insert_now.append(order)
                continue
            holder_id = _oid(holder)
            if holder_id in blob_ids:
                held_by_blob.append({"insert_id": _oid(order), "holder_id": holder_id, "display_seq": _seq(order)})
            else:
                blocked.append(
                    {
                        "insert_id": _oid(order),
                        "orphan_id": holder_id,
                        "display_seq": _seq(order),
                    }
                )
        buys = [o for o in inserts if _side(o) == "buy"]
        sells = [o for o in inserts if _side(o) == "sell"]
        block["tenants"][tenant] = {
            "archive_count": len(orphans),
            "insert_count": len(inserts),
            "buys": len(buys),
            "sells": len(sells),
            "buy_usdt": str(sum((_usdt(o) for o in buys), Decimal("0.00"))),
            "sell_usdt": str(sum((_usdt(o) for o in sells), Decimal("0.00"))),
            "insertable_now": len(insert_now),
            "blocked": len(blocked),
            "blocked_pairs": blocked,
            "held_by_blob": held_by_blob,
            "filled_orphan_ids": [_oid(d) for d in filled_orphans],
            "archive_ids": sorted(_oid(d) for d in orphans),
            "insert_ids": sorted(_oid(o) for o in inserts),
            "blob_count": len(blob),
            "blob_max_seq": _max_seq(blob),
            "v2_count": len(v2),
            "v2_max_seq": _max_seq(v2),
            "dry_run_fill_ids": sorted(_oid(d) for d in orphans if _dry_run_fill(d)),
        }
    return block


def read_idempotency_index(db) -> dict | None:
    if ORDERS_V2_COLLECTION not in set(db.list_collection_names()):
        return None
    info = db[ORDERS_V2_COLLECTION].index_information()
    return info.get("idempotency_key")


def index_is_partial(info: dict | None) -> bool:
    from storage.order_ledger_v2 import _idempotency_index_is_partial_nonempty

    return _idempotency_index_is_partial_nonempty(info)


def _strip_archive(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k not in ARCHIVE_FIELDS}


def _same_body(left: dict, right: dict) -> bool:
    return _strip_archive(left) == _strip_archive(right)


def _norm_num(value: object) -> str:
    if value is None or value == "":
        return ""
    try:
        return str(Decimal(str(value)).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP))
    except Exception:
        return str(value)


def fields_match(blob: dict, v2: dict) -> list[str]:
    """Field differences for one order id. display_seq is a field, not the key."""
    bad = []
    if _side(blob) != _side(v2):
        bad.append("side")
    if _norm_num(_field(blob, "amount")) != _norm_num(_field(v2, "amount")):
        bad.append("amount")
    if _norm_num(_field(blob, "price")) != _norm_num(_field(v2, "price")):
        bad.append("price")
    if _norm_num(_field(blob, "fee")) != _norm_num(_field(v2, "fee")):
        bad.append("fee")
    if _fill_stamp(blob) != _fill_stamp(v2):
        bad.append("fill_time")
    if str(blob.get("status") or "") != str(v2.get("status") or ""):
        bad.append("status")
    if _seq(blob) != _seq(v2):
        bad.append("display_seq")
    return bad


def _lots(orders: list[dict]) -> dict[str, str]:
    from core.sim_ledger_replay import replay_simulated_ledger

    filled = [o for o in orders if str(o.get("status") or "") == "filled"]
    snap = replay_simulated_ledger(filled, initial=1_000_000.0)["positions"]
    out = {}
    for key, pos in snap.items():
        amount = float(pos.get("amount") or 0)
        if amount > 1e-12:
            out[str(key)] = f"{amount:.8f}"
    return out


def verify_parity(db, tenants: list[str], scope: str) -> dict:
    """R2. Key is (tenant_id, ledger_scope, order_id). display_seq is compared."""
    report: dict[str, Any] = {"key": "tenant_id,ledger_scope,order_id", "tenants": {}, "green": True}
    for tenant in tenants:
        blob = blob_orders(db, tenant, scope)
        v2 = v2_orders(db, tenant, scope)
        blob_by_id = {_oid(o): o for o in blob if _oid(o)}
        v2_by_id = {_oid(d): d for d in v2 if _oid(d)}
        blob_seqs = {_seq(o): _oid(o) for o in blob}
        missing = []
        mismatched = []
        status_differs = []
        for oid, order in blob_by_id.items():
            other = v2_by_id.get(oid)
            if other is None:
                if _filled(order):
                    missing.append(oid)
                continue
            if str(order.get("status") or "") != str(other.get("status") or ""):
                status_differs.append(oid)
            if _filled(order):
                bad = fields_match(order, other)
                if bad:
                    mismatched.append({"order_id": oid, "fields": bad})
        filled_only_v2 = [
            oid for oid, doc in v2_by_id.items() if _filled(doc) and oid not in blob_by_id
        ]
        orphans_holding = []
        for doc in v2:
            seq = _seq(doc)
            holder = blob_seqs.get(seq)
            if holder and holder != _oid(doc):
                orphans_holding.append(_oid(doc))
        blob_lots = _lots(blob)
        v2_lots = _lots(v2)
        lot_keys = set(blob_lots) | set(v2_lots)
        lots_differing = [k for k in sorted(lot_keys) if blob_lots.get(k) != v2_lots.get(k)]
        row = {
            "blob": len(blob),
            "v2": len(v2),
            "missing": len(missing),
            "mismatched": len(mismatched),
            "filled_only_v2": len(filled_only_v2),
            "status_differs": len(status_differs),
            "lots_differing": len(lots_differing),
            "orphans_holding_blob_number": len(orphans_holding),
            "missing_ids": missing,
            "mismatched_detail": mismatched,
            "status_differs_ids": status_differs,
        }
        if any(row[k] for k in (
            "missing", "mismatched", "filled_only_v2", "status_differs",
            "lots_differing", "orphans_holding_blob_number",
        )):
            report["green"] = False
        report["tenants"][tenant] = row
    return report


def _logical_found(db, found: dict, expected: dict, scope: str, run_id: str) -> dict:
    """Treat work this run already finished as part of the dry-run set.

    A second ``--apply`` then matches the same expected_counts and writes 0.
    """
    import copy

    out = copy.deepcopy(found)
    archived_ids: set[str] = set()
    name = archive_name(run_id) if run_id else ""
    if name and name in set(db.list_collection_names()):
        archived_ids = {str(doc.get("id") or "") for doc in db[name].find()}
    for tenant, row in out["tenants"].items():
        exp = (expected.get("tenants") or {}).get(tenant) or {}
        v2_ids = {_oid(doc) for doc in v2_orders(db, tenant, scope)}
        blob = {_oid(order): order for order in blob_orders(db, tenant, scope)}
        logical_arch = set(row["archive_ids"]) | {
            oid for oid in exp.get("archive_ids") or [] if oid in archived_ids and oid not in v2_ids
        }
        logical_ins = set(row["insert_ids"]) | {
            oid for oid in exp.get("insert_ids") or [] if oid in v2_ids
        }
        row["archive_ids"] = sorted(logical_arch)
        row["archive_count"] = len(logical_arch)
        row["insert_ids"] = sorted(logical_ins)
        row["insert_count"] = len(logical_ins)
        chosen = [blob[oid] for oid in logical_ins if oid in blob]
        buys = [order for order in chosen if _side(order) == "buy"]
        sells = [order for order in chosen if _side(order) == "sell"]
        row["buys"] = len(buys)
        row["sells"] = len(sells)
        row["buy_usdt"] = str(sum((_usdt(order) for order in buys), Decimal("0.00")))
        row["sell_usdt"] = str(sum((_usdt(order) for order in sells), Decimal("0.00")))
        row["v2_count"] = exp.get("v2_count", row["v2_count"])
        row["v2_max_seq"] = exp.get("v2_max_seq", row["v2_max_seq"])
        row["insertable_now"] = exp.get("insertable_now", row["insertable_now"])
        row["blocked"] = exp.get("blocked", row["blocked"])
    return out


def _diffs(expected: dict, found: dict) -> list[str]:
    problems = []
    if (expected or {}).get("as_of") != (found or {}).get("as_of"):
        problems.append(
            f"DIFF field=as_of expected={expected.get('as_of')} found={found.get('as_of')}"
        )
    exp_tenants = (expected or {}).get("tenants") or {}
    found_tenants = (found or {}).get("tenants") or {}
    if set(exp_tenants) != set(found_tenants):
        problems.append(
            f"DIFF field=tenants expected={sorted(exp_tenants)} found={sorted(found_tenants)}"
        )
    for tenant in sorted(set(exp_tenants) & set(found_tenants)):
        exp = exp_tenants[tenant]
        got = found_tenants[tenant]
        for key in ("archive_count", "insert_count", "buys", "sells", "insertable_now", "blocked",
                    "blob_count", "blob_max_seq", "v2_count", "v2_max_seq"):
            if exp.get(key) != got.get(key):
                problems.append(
                    f"DIFF tenant={tenant} field={key} expected={exp.get(key)} found={got.get(key)}"
                )
        for key in ("buy_usdt", "sell_usdt"):
            if str(exp.get(key)) != str(got.get(key)):
                problems.append(
                    f"DIFF tenant={tenant} field={key} expected={exp.get(key)} found={got.get(key)}"
                )
        for key in ("archive_ids", "insert_ids"):
            exp_ids = set(exp.get(key) or [])
            got_ids = set(got.get(key) or [])
            if exp_ids != got_ids:
                problems.append(
                    f"DIFF tenant={tenant} field={key} "
                    f"missing={sorted(exp_ids - got_ids)} extra={sorted(got_ids - exp_ids)}"
                )
    return problems


def _progress_ok(db, expected: dict, scope: str) -> list[str]:
    """Live counts must be the dry-run baseline minus work this run already did."""
    problems = []
    run_id = expected.get("run_id")
    archive = archive_name(run_id) if run_id else ""
    archived_ids: set[str] = set()
    if archive and archive in set(db.list_collection_names()):
        archived_ids = {str(d.get("id") or "") for d in db[archive].find()}
    for tenant, exp in (expected.get("tenants") or {}).items():
        blob = blob_orders(db, tenant, scope)
        v2 = v2_orders(db, tenant, scope)
        v2_ids = {_oid(d) for d in v2}
        if len(blob) != int(exp["blob_count"]) or _max_seq(blob) != int(exp["blob_max_seq"]):
            problems.append(
                f"DIFF tenant={tenant} field=blob_baseline "
                f"expected_count={exp['blob_count']} found_count={len(blob)} "
                f"expected_max={exp['blob_max_seq']} found_max={_max_seq(blob)}"
            )
        done_archive = len([i for i in exp.get("archive_ids") or [] if i in archived_ids and i not in v2_ids])
        done_insert = len([i for i in exp.get("insert_ids") or [] if i in v2_ids])
        expect_v2 = int(exp["v2_count"]) - done_archive + done_insert
        if len(v2) != expect_v2 or _max_seq(v2) != int(exp["v2_max_seq"]):
            problems.append(
                f"DIFF tenant={tenant} field=v2_baseline "
                f"expected_count={expect_v2} found_count={len(v2)} "
                f"expected_max={exp['v2_max_seq']} found_max={_max_seq(v2)}"
            )
    return problems


def _end_ok(db, expected: dict, scope: str, hashes_before: dict[str, str]) -> list[str]:
    problems = []
    run_id = expected["run_id"]
    name = archive_name(run_id)
    for tenant, exp in expected["tenants"].items():
        blob = blob_orders(db, tenant, scope)
        v2 = v2_orders(db, tenant, scope)
        if len(blob) != int(exp["blob_count"]) or _max_seq(blob) != int(exp["blob_max_seq"]):
            problems.append(f"END tenant={tenant} blob count or max display_seq changed")
        expect_v2 = int(exp["v2_count"]) - int(exp["archive_count"]) + int(exp["insert_count"])
        if len(v2) != expect_v2:
            problems.append(
                f"END tenant={tenant} v2_count={len(v2)} expected={expect_v2}"
            )
        if _max_seq(v2) != int(exp["v2_max_seq"]):
            problems.append(f"END tenant={tenant} v2 max display_seq changed")
        archived = 0
        if name in set(db.list_collection_names()):
            archived = db[name].count_documents({"tenant_id": tenant})
        if archived != int(exp["archive_count"]):
            problems.append(
                f"END tenant={tenant} archive_count={archived} expected={exp['archive_count']}"
            )
        audits = db[AUDIT_COLLECTION].count_documents({"run_id": run_id, "tenant_id": tenant})
        if audits != 1:
            problems.append(f"END tenant={tenant} audit_records={audits} expected=1")
    now = hash_ledger(db, run_id)
    for coll in (POSITIONS_COLLECTION, TRADE_HISTORY_COLLECTION, ORDERS_COLLECTION):
        if now.get(coll) != hashes_before.get(coll):
            problems.append(f"END collection {coll} hash changed")
    return problems


class _Proxy:
    def __init__(self, inner, hooks: ApplyHooks):
        self._inner = inner
        self._hooks = hooks

    def upsert_order(self, order: dict) -> None:
        oid = str(order.get("id") or "")
        if oid in self._hooks.fail_upsert_ids:
            raise RuntimeError(f"forced upsert failure {oid}")
        self._inner.upsert_order(order)

    def get_by_id(self, tenant_id: str, scope: str, order_id: str):
        if order_id in self._hooks.fail_readback_ids:
            return None
        return self._inner.get_by_id(tenant_id, scope, order_id)

    def rebuild_day_stats(self, tenant_id: str, scope: str, day_key: str):
        return self._inner.rebuild_day_stats(tenant_id, scope, day_key)


def _obtain_store(hooks: ApplyHooks | None):
    from storage.order_ledger_v2 import (
        get_order_ledger_v2,
        order_ledger_v2_is_degraded,
        reset_order_ledger_v2_for_tests,
    )

    if hooks and hooks.store_none:
        return None
    backend = (os.environ.get("ORDER_LEDGER_V2_BACKEND") or "").strip().lower()
    if backend != "mongo" or os.environ.get("ORDER_LEDGER_V2", "1").strip().lower() in {"0", "false", "off", "no"}:
        return None
    reset_order_ledger_v2_for_tests()
    try:
        store = get_order_ledger_v2()
    except Exception:
        return None
    if hooks and hooks.degraded:
        import storage.order_ledger_v2 as mod

        mod._V2_DEGRADED = True
    if store is None or order_ledger_v2_is_degraded():
        return None
    if type(store).__name__ != "MongoOrderLedgerV2":
        return None
    if hooks:
        return _Proxy(store, hooks)
    return store


def _guard_db(db, operator_cleared: bool) -> None:
    name = getattr(db, "name", "")
    if operator_cleared:
        if name != OPERATOR_DB:
            raise _Abort(f"C1: db is not {OPERATOR_DB}")
        return
    if name in {OPERATOR_DB, PROD_DB}:
        raise _Abort("refusing operator database without a cleared C1 gate")


def _require_apply_preface(
    *,
    command: str,
    summary: dict | None,
    script_sha256_expected: str | None,
    backup_path: str | None,
    backup_sha256: str | None,
    run_id: str | None,
) -> None:
    if not command.strip():
        raise _Abort("missing command log")
    if not isinstance(summary, dict) or "db" not in summary or "host" not in summary:
        raise _Abort("missing operator_mongo_summary")
    if not script_sha256_expected:
        raise _Abort("missing script sha256")
    if script_sha256_expected.strip().lower() != script_sha256():
        raise _Abort("script sha256 does not match")
    if not backup_path or not backup_sha256:
        raise _Abort("missing collection hashes")
    if not run_id:
        raise _Abort("missing run id")


def _check_hashes(db, run_id: str, backup_path: str, backup_sha: str) -> dict[str, str]:
    expected = load_backup_manifest(backup_path, backup_sha)
    live = hash_ledger(db, run_id)
    for name in ledger_collection_names(run_id):
        if name not in expected:
            raise _Abort(f"missing collection hash for {name}")
        if expected[name] != live[name]:
            raise _Abort(f"collection hash mismatch for {name}")
    return live


def _index_or_abort(db, lines: list[str]) -> None:
    info = read_idempotency_index(db)
    ok = index_is_partial(info)
    lines.append(
        "INDEX idempotency_key "
        f"present={info is not None} unique={bool((info or {}).get('unique'))} "
        f"sparse={bool((info or {}).get('sparse'))} partial_nonempty={ok}"
    )
    if not ok:
        raise _Abort("idempotency index is not partial-nonempty")


def _emit_found(lines: list[str], found: dict) -> None:
    public = json.loads(json.dumps(found))
    lines.append("EXPECTED_COUNTS " + json.dumps(public, sort_keys=True))
    for tenant, row in found["tenants"].items():
        lines.append(
            f"TENANT {tenant} archive={row['archive_count']} insert={row['insert_count']} "
            f"buys={row['buys']} buy_usdt={row['buy_usdt']} sells={row['sells']} "
            f"sell_usdt={row['sell_usdt']} insertable_now={row['insertable_now']} "
            f"blocked={row['blocked']} blob_max_seq={row['blob_max_seq']} "
            f"v2_max_seq={row['v2_max_seq']}"
        )
        for pair in row["blocked_pairs"]:
            lines.append(
                f"BLOCKED tenant={tenant} display_seq={pair['display_seq']} "
                f"orphan_id={pair['orphan_id']} insert_id={pair['insert_id']}"
            )


def _finish(lines: list[str], *, code: int, done: str, not_done: str, found=None, verify=None) -> JobResult:
    lines.append(f"DONE {done}")
    lines.append(f"NOT DONE {not_done}")
    for line in lines:
        print(line)
    return JobResult(code, lines, found, verify)


def run_job(
    db,
    *,
    mode: str,
    tenants: list[str],
    scope: str,
    run_id: str | None = None,
    as_of: str | None = None,
    expected: dict | None = None,
    backup_path: str | None = None,
    backup_sha256: str | None = None,
    script_sha256_expected: str | None = None,
    command: str = "",
    summary: dict | None = None,
    operator_cleared: bool = False,
    hooks: ApplyHooks | None = None,
) -> JobResult:
    lines: list[str] = []
    counters = {"archived": 0, "inserted": 0, "deleted": 0}
    done = "archived=0 inserted=0 deleted=0 writes=0 archives=0"
    try:
        _guard_db(db, operator_cleared)
        lines.append(f"COMMAND {command}")
        lines.append(f"RUN_ID {run_id or ''}")
        lines.append(f"SCRIPT_SHA256 {script_sha256()}")
        lines.append(f"OPERATOR_MONGO {format_summary(summary)}")
        if mode in {"apply", "rollback"}:
            _require_apply_preface(
                command=command,
                summary=summary,
                script_sha256_expected=script_sha256_expected,
                backup_path=backup_path,
                backup_sha256=backup_sha256,
                run_id=run_id,
            )
            hashes_before = _check_hashes(db, run_id or "", backup_path or "", backup_sha256 or "")
            lines.append("HASHES " + json.dumps(hashes_before, sort_keys=True))
        else:
            hashes_before = {}
        if mode in {"dry-run", "apply", "rollback"}:
            _index_or_abort(db, lines)
        if mode == "verify":
            report = verify_parity(db, tenants, scope)
            for tenant, row in report["tenants"].items():
                lines.append(
                    f"R2 tenant={tenant} key={report['key']} blob={row['blob']} v2={row['v2']} "
                    f"missing={row['missing']} mismatched={row['mismatched']} "
                    f"filled_only_v2={row['filled_only_v2']} status_differs={row['status_differs']} "
                    f"lots_differing={row['lots_differing']} "
                    f"orphans_holding_blob_number={row['orphans_holding_blob_number']}"
                )
            lines.append(f"OPERATOR_MONGO {format_summary(summary)}")
            code = 0 if report["green"] else 2
            not_done = "none" if report["green"] else "R2 is not green"
            return _finish(lines, code=code, done=done, not_done=not_done, verify=report)
        if mode == "dry-run":
            found = survey(db, tenants, scope, as_of)
            _emit_found(lines, found)
            lines.append(f"OPERATOR_MONGO {format_summary(summary)}")
            return _finish(lines, code=0, done=done, not_done="none", found=found)
        if mode == "rollback":
            return _rollback(db, lines, run_id or "", tenants, scope, summary, hashes_before)
        if mode != "apply":
            raise _Abort(f"unknown mode {mode}")
        if not expected:
            raise _Abort("missing expected_counts")
        expected = dict(expected)
        expected["run_id"] = run_id
        if as_of and expected.get("as_of") not in {None, as_of}:
            raise _Abort(f"DIFF field=as_of expected={expected.get('as_of')} found={as_of}")
        found = survey(db, tenants, scope, expected.get("as_of") or as_of)
        for tenant, row in found["tenants"].items():
            if row["filled_orphan_ids"]:
                raise _Abort(
                    "filled orphan not in the blob: "
                    + ",".join(row["filled_orphan_ids"])
                    + "; nothing archived, nothing written"
                )
            if row["held_by_blob"]:
                held = row["held_by_blob"][0]
                raise _Abort(
                    "target display_seq held by a non-orphan "
                    f"tenant={tenant} display_seq={held['display_seq']} holder={held['holder_id']}; "
                    "nothing archived, nothing written"
                )
        logical = _logical_found(db, found, expected, scope, run_id or "")
        problems = _diffs(expected, logical)
        if problems:
            raise _Abort("expected_counts mismatch: " + "; ".join(problems))
        drift = _progress_ok(db, expected, scope)
        if drift:
            raise _Abort("baseline changed before write: " + "; ".join(drift))
        store = _obtain_store(hooks)
        lines.append(f"V2_STORE {type(store._inner).__name__ if isinstance(store, _Proxy) else type(store).__name__}")
        if store is None:
            raise _Abort("v2 store unavailable or degraded; nothing archived, nothing written")
        _apply(db, store, expected, scope, hooks, counters, lines)
        drift_end = _end_ok(db, expected, scope, hashes_before)
        lines.append("HASHES_END " + json.dumps(hash_ledger(db, run_id), sort_keys=True))
        lines.append(f"OPERATOR_MONGO {format_summary(summary)}")
        done = (
            f"archived={counters['archived']} inserted={counters['inserted']} "
            f"deleted={counters['deleted']} writes={counters['inserted']} archives={counters['archived']}"
        )
        if drift_end:
            raise _Abort("end state mismatch: " + "; ".join(drift_end))
        return _finish(lines, code=0, done=done, not_done="none", found=found)
    except _Abort as exc:
        done = (
            f"archived={counters['archived']} inserted={counters['inserted']} "
            f"deleted={counters['deleted']} writes={counters['inserted']} archives={counters['archived']}"
        )
        reason = exc.reason
        nothing = "nothing archived, nothing written"
        if nothing not in reason and counters["archived"] == 0 and counters["inserted"] == 0 and counters["deleted"] == 0:
            not_done = f"{reason}; {nothing}"
        else:
            not_done = reason
        return _finish(lines, code=2, done=done, not_done=not_done)


def _apply(db, store, expected: dict, scope: str, hooks: ApplyHooks | None, counters: dict, lines: list[str]) -> None:
    run_id = expected["run_id"]
    name = archive_name(run_id)
    archive = db[name]
    v2 = db[ORDERS_V2_COLLECTION]
    hooks = hooks or ApplyHooks()
    mutated = False
    for tenant, exp in expected["tenants"].items():
        blob = blob_orders(db, tenant, scope)
        blob_by_id = {_oid(o): o for o in blob}
        blob_by_seq = {_seq(o): o for o in blob}
        wanted = set(exp["archive_ids"])
        for doc in v2_orders(db, tenant, scope):
            if _oid(doc) not in wanted:
                continue
            existing = archive.find_one({"_id": doc["_id"]})
            if existing is not None and not _same_body(existing, doc):
                raise _Abort(
                    f"archive copy differs for {_oid(doc)}; nothing further archived, nothing removed"
                )
        for doc in v2_orders(db, tenant, scope):
            oid = _oid(doc)
            if oid not in wanted:
                continue
            holder = blob_by_seq.get(_seq(doc))
            payload = dict(doc)
            payload["archived_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            payload["reason"] = ARCHIVE_REASON
            payload["blob_order_id"] = _oid(holder) if holder else None
            existing = archive.find_one({"_id": doc["_id"]})
            if existing is None:
                archive.insert_one(payload)
                counters["archived"] += 1
            elif not _same_body(existing, payload):
                raise _Abort(f"archive copy differs for {oid}")
            readback = archive.find_one({"_id": doc["_id"]})
            if oid in hooks.fail_archive_readback or readback is None or not _same_body(readback, doc):
                raise _Abort(f"archive read-back failed for {oid}; nothing removed")
            if hooks.mutate_v2_before_delete and not mutated:
                v2.update_one({"_id": doc["_id"]}, {"$set": {"status": "mutated-before-delete"}})
                mutated = True
            current = v2.find_one({"_id": doc["_id"]})
            if current is None:
                continue
            if not _same_body(current, doc):
                raise _Abort(f"v2 orphan changed before delete {_oid(doc)}")
            if hooks.deleted_count_override is not None:
                deleted = hooks.deleted_count_override
            else:
                deleted = v2.delete_one({"_id": doc["_id"]}).deleted_count
            if deleted != 1:
                raise _Abort(f"deleted_count={deleted} for {_oid(doc)}")
            counters["deleted"] += 1
        _write_audit(db, tenant, scope, run_id, name, exp, blob_by_id)
    if hooks.abort_after_delete:
        raise _Abort("aborted after archive delete and before insert")
    if hooks.after_archive:
        hooks.after_archive()
    drift = _progress_ok(db, expected, scope)
    if drift:
        raise _Abort("baseline changed during run: " + "; ".join(drift))
    if hooks.before_insert:
        hooks.before_insert()
    for tenant, exp in expected["tenants"].items():
        blob = {_oid(o): o for o in blob_orders(db, tenant, scope)}
        for oid in exp["insert_ids"]:
            order = blob.get(oid)
            if order is None:
                raise _Abort(f"insert id {oid} is not in the blob")
            current = store.get_by_id(tenant, scope, oid)
            if current is not None:
                if not fields_match(order, current):
                    continue
                raise _Abort(f"insert target already exists for {oid}; nothing replaced")
            record = dict(order)
            record["tenant_id"] = tenant
            record["ledger_scope"] = scope
            from storage.order_ledger_v2 import omit_empty_idempotency_key

            try:
                store.upsert_order(omit_empty_idempotency_key(record))
            except _Abort:
                raise
            except Exception as exc:
                raise _Abort(f"upsert failed for {oid}: {exc}") from exc
            readback = store.get_by_id(tenant, scope, oid)
            if readback is None or _seq(readback) != _seq(order) or str(readback.get("status") or "") != str(order.get("status") or ""):
                raise _Abort(f"read-back failed for {oid}")
            counters["inserted"] += 1
        days = set(JULY_DAYS)
        for oid in exp["insert_ids"]:
            day = str((blob.get(oid) or {}).get("day_key") or "")
            if day:
                days.add(day)
        for day in sorted(days):
            store.rebuild_day_stats(tenant, scope, day)
    if hooks.after_writes:
        hooks.after_writes()


def _write_audit(db, tenant: str, scope: str, run_id: str, archive: str, exp: dict, blob_by_id: dict) -> None:
    from storage.order_ledger_v2 import compound_order_id

    doc_id = f"repair|{tenant}|v2|{run_id}"
    inserted_ids = [compound_order_id(tenant, scope, oid) for oid in exp["insert_ids"]]
    days = sorted({str((blob_by_id.get(oid) or {}).get("day_key") or "") for oid in exp["insert_ids"] if blob_by_id.get(oid)} - {""})
    payload = {
        "_id": doc_id,
        "record": doc_id,
        "tenant_id": tenant,
        "ledger_scope": scope,
        "run_id": run_id,
        "archive_collection": archive,
        "archived_order_ids": list(exp["archive_ids"]),
        "inserted_order_ids": list(exp["insert_ids"]),
        "inserted_ids": inserted_ids,
        "dry_run_fill_order_ids": list(exp.get("dry_run_fill_ids") or []),
        "insert_day_keys": days,
        "july_days": list(JULY_DAYS),
    }
    db[AUDIT_COLLECTION].replace_one({"_id": doc_id}, payload, upsert=True)


def _rollback(db, lines, run_id, tenants, scope, summary, hashes_before) -> JobResult:
    name = archive_name(run_id)
    if name not in set(db.list_collection_names()):
        raise _Abort("archive collection is missing; nothing restored")
    audits = list(db[AUDIT_COLLECTION].find({"run_id": run_id}))
    inserted_ids: list[str] = []
    days: set[str] = set(JULY_DAYS)
    for audit in audits:
        inserted_ids.extend(audit.get("inserted_ids") or [])
        days.update(audit.get("insert_day_keys") or [])
    v2 = db[ORDERS_V2_COLLECTION]
    removed = 0
    for doc_id in inserted_ids:
        result = v2.delete_one({"_id": doc_id})
        removed += result.deleted_count
    restored = 0
    for doc in list(db[name].find().sort("_id", 1)):
        body = _strip_archive(doc)
        current = v2.find_one({"_id": body["_id"]})
        if current is None:
            v2.insert_one(body)
        elif not _same_body(current, body):
            raise _Abort(f"rollback refused: v2 already holds a different {_oid(body)}")
        readback = v2.find_one({"_id": body["_id"]})
        if readback is None or not _same_body(readback, body):
            raise _Abort(f"rollback read-back failed for {_oid(body)}")
        restored += 1
    db.drop_collection(name)
    db[AUDIT_COLLECTION].delete_many({"run_id": run_id})
    store = _obtain_store(None)
    if store is None:
        raise _Abort("v2 store unavailable during rollback day-stat rebuild")
    for tenant in tenants:
        for day in sorted(days):
            store.rebuild_day_stats(tenant, scope, day)
    lines.append(f"ROLLBACK restored={restored} removed_inserts={removed}")
    lines.append("HASHES_END " + json.dumps(hash_ledger(db, run_id), sort_keys=True))
    lines.append(f"OPERATOR_MONGO {format_summary(summary)}")
    # The archive is gone, so its hash is the empty digest. positions/trade/blob stay.
    now = hash_ledger(db, None)
    for coll in (POSITIONS_COLLECTION, TRADE_HISTORY_COLLECTION, ORDERS_COLLECTION):
        if now.get(coll) != hashes_before.get(coll):
            raise _Abort(f"rollback changed {coll}")
    done = f"archived=0 inserted=0 deleted=0 writes=0 archives=0 restored={restored}"
    return _finish(lines, code=0, done=done, not_done="none")


def _load_expected(path: str | None) -> dict | None:
    if not path:
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill filled blob orders into orders_v2 and verify parity")
    parser.add_argument("--tenants", default="default,henry")
    parser.add_argument("--scope", default="demo")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--dump-hashes", default=None, help="Write a raw-bson hash manifest and exit")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--as-of", default=None)
    parser.add_argument("--expected-counts", default=None)
    parser.add_argument("--backup-hashes", default=None)
    parser.add_argument("--backup-sha256", default=None)
    parser.add_argument("--script-sha256", default=None)
    parser.add_argument("--dry-run", action="store_true", help="Default. Explicit dry run.")
    args = parser.parse_args(argv)
    command_argv = list(argv) if argv is not None else sys.argv[1:]
    command = " ".join(command_argv)
    summary, err = operator_gate()
    if err:
        redacted = dict(summary or {})
        redacted["host"] = "redacted"
        print(f"OPERATOR_MONGO {format_summary(redacted)}")
        print("DONE archived=0 inserted=0 deleted=0 writes=0 archives=0")
        print(f"NOT DONE {err}; nothing archived, nothing written")
        return 2
    print(f"OPERATOR_MONGO {format_summary(summary)}")
    from storage.mongo_client import get_database

    db = get_database()
    tenants = [part.strip() for part in args.tenants.split(",") if part.strip()]
    if args.dump_hashes:
        digest = write_backup_manifest(db, args.dump_hashes, args.run_id)
        print(f"BACKUP_SHA256 {digest}")
        print(f"SCRIPT_SHA256 {script_sha256()}")
        return 0
    if args.verify:
        mode = "verify"
    elif args.rollback:
        mode = "rollback"
    elif args.apply:
        mode = "apply"
    else:
        mode = "dry-run"
    result = run_job(
        db,
        mode=mode,
        tenants=tenants,
        scope=args.scope,
        run_id=args.run_id,
        as_of=args.as_of,
        expected=_load_expected(args.expected_counts),
        backup_path=args.backup_hashes,
        backup_sha256=args.backup_sha256,
        script_sha256_expected=args.script_sha256,
        command=command,
        summary=summary,
        operator_cleared=True,
    )
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
