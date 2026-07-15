#!/usr/bin/env python3
"""Repair split tenant ledgers after MT leakage (legacy paper vs default:paper vs henry:paper)."""

from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.tenant_context import DEFAULT_TENANT
from storage.mongo_ledger import (
    ORDERS_COLLECTION,
    POSITIONS_COLLECTION,
    TRADE_HISTORY_COLLECTION,
    MongoLedgerStore,
)
from storage.tenant_keys import compound_ledger_id, is_legacy_doc

COLLECTIONS = (
    (ORDERS_COLLECTION, "orders"),
    (POSITIONS_COLLECTION, "positions"),
    (TRADE_HISTORY_COLLECTION, "trades"),
)


def _entry_ids(doc: dict | None, payload_key: str) -> set[str]:
    if not doc:
        return set()
    if payload_key == "positions":
        return set((doc.get("positions") or {}).keys())
    return {str(x.get("id")) for x in (doc.get(payload_key) or []) if x.get("id")}


def _payload_size(doc: dict | None, payload_key: str) -> int:
    if not doc:
        return 0
    payload = doc.get(payload_key)
    if payload_key == "positions":
        return len(payload or {})
    return len(payload or [])


def migrate_operator_legacy_to_compound(
    *,
    scope: str = "paper",
    dry_run: bool = True,
    test: bool = False,
) -> dict:
    """Copy legacy scope docs into default:<scope> when compound is missing or thinner."""
    store = MongoLedgerStore(test=test)
    stats: dict = {"scope": scope, "dry_run": dry_run, "collections": {}}

    for coll_name, payload_key in COLLECTIONS:
        coll = store._collection(coll_name)
        legacy = coll.find_one({"_id": scope})
        if not legacy or not is_legacy_doc(legacy):
            continue
        compound_id = compound_ledger_id(DEFAULT_TENANT, scope)
        compound = coll.find_one({"_id": compound_id}) or {}
        legacy_n = _payload_size(legacy, payload_key)
        compound_n = _payload_size(compound, payload_key)
        if legacy_n <= compound_n:
            stats["collections"][coll_name] = {
                "skipped": True,
                "legacy_entries": legacy_n,
                "compound_entries": compound_n,
            }
            continue
        stats["collections"][coll_name] = {
            "legacy_entries": legacy_n,
            "compound_entries_before": compound_n,
            "compound_entries_after": legacy_n,
        }
        if dry_run:
            continue
        payload = copy.deepcopy(legacy)
        payload["_id"] = compound_id
        payload["tenant_id"] = DEFAULT_TENANT
        payload["ledger_scope"] = scope
        coll.replace_one({"_id": compound_id}, payload, upsert=True)

    return stats


def repair_tenant_ledgers(
    *,
    scope: str = "paper",
    target_tenant: str = "henry",
    dry_run: bool = True,
    test: bool = False,
) -> dict:
    store = MongoLedgerStore(test=test)
    stats: dict = {"scope": scope, "target": target_tenant, "dry_run": dry_run, "collections": {}}

    for coll_name, payload_key in COLLECTIONS:
        coll = store._collection(coll_name)
        legacy = coll.find_one({"_id": scope})
        default_doc = coll.find_one({"_id": compound_ledger_id(DEFAULT_TENANT, scope)}) or {}
        target_doc = coll.find_one({"_id": compound_ledger_id(target_tenant, scope)}) or {}

        legacy_ok = legacy and is_legacy_doc(legacy)
        if not legacy_ok and coll_name == ORDERS_COLLECTION:
            stats["collections"][coll_name] = {
                "skipped": True,
                "reason": "no_legacy_scope_doc",
            }
            continue
        legacy_ids = _entry_ids(legacy if legacy_ok else None, payload_key)

        to_target: list | dict = [] if payload_key != "positions" else {}
        if legacy_ok:
            operator_payload: list | dict = copy.deepcopy(
                legacy.get(payload_key) or ([] if payload_key != "positions" else {})
            )
        else:
            operator_payload = [] if payload_key != "positions" else {}

        if payload_key == "positions":
            operator_ids = set(operator_payload.keys())
        else:
            operator_ids = _entry_ids({payload_key: operator_payload}, payload_key)
        default_payload = default_doc.get(payload_key) or ([] if payload_key != "positions" else {})
        if payload_key == "positions":
            for key, pos in (default_payload or {}).items():
                if key in legacy_ids or key in operator_ids:
                    operator_payload[key] = copy.deepcopy(pos)
                    operator_ids.add(key)
                else:
                    to_target[key] = copy.deepcopy(pos)
        else:
            for entry in default_payload or []:
                entry_id = str(entry.get("id") or "")
                if entry_id and (entry_id in legacy_ids or entry_id in operator_ids):
                    if entry_id not in operator_ids:
                        operator_payload.append(copy.deepcopy(entry))
                        operator_ids.add(entry_id)
                else:
                    to_target.append(copy.deepcopy(entry))

        target_existing = copy.deepcopy(target_doc.get(payload_key) or ([] if payload_key != "positions" else {}))
        if payload_key == "positions":
            for key in list(target_existing.keys()):
                if key in legacy_ids or key in operator_ids:
                    target_existing.pop(key, None)
            moved = len(to_target)
            target_existing.update(to_target)
            operator_count = len(operator_payload)
            target_count = len(target_existing)
        else:
            cleaned_target = []
            for entry in target_existing if isinstance(target_existing, list) else []:
                entry_id = str(entry.get("id") or "")
                tagged = str(entry.get("tenant_id") or "").strip()
                if tagged and tagged != target_tenant:
                    continue
                if entry_id and entry_id in legacy_ids:
                    continue
                cleaned_target.append(entry)
            target_existing = cleaned_target
            existing_target_ids = _entry_ids({payload_key: target_existing}, payload_key)
            moved = 0
            for entry in to_target:
                entry_id = str(entry.get("id") or "")
                if entry_id and entry_id in existing_target_ids:
                    continue
                entry = copy.deepcopy(entry)
                entry["tenant_id"] = target_tenant
                target_existing.append(entry)
                existing_target_ids.add(entry_id)
                moved += 1
            operator_count = len(operator_payload)
            target_count = len(target_existing)

        stats["collections"][coll_name] = {
            "legacy_entries": len(legacy_ids),
            "moved_to_target": moved,
            "operator_entries": operator_count,
            "target_entries_after": target_count,
        }

        if dry_run:
            continue

        if legacy_ok or operator_count or default_doc:
            restored = copy.deepcopy(legacy if legacy_ok else default_doc)
            restored[payload_key] = operator_payload
            restored["tenant_id"] = DEFAULT_TENANT
            restored["ledger_scope"] = scope
            restored["_id"] = compound_ledger_id(DEFAULT_TENANT, scope)
            coll.replace_one({"_id": restored["_id"]}, restored, upsert=True)

        if moved or target_doc:
            target_out = copy.deepcopy(target_doc) if target_doc else {
                "tenant_id": target_tenant,
                "ledger_scope": scope,
            }
            target_out[payload_key] = target_existing
            target_out["tenant_id"] = target_tenant
            target_out["ledger_scope"] = scope
            target_out["_id"] = compound_ledger_id(target_tenant, scope)
            coll.replace_one({"_id": target_out["_id"]}, target_out, upsert=True)

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", default="paper")
    parser.add_argument("--target", default="henry", help="Tenant that received leaked default rows")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--test-db", action="store_true")
    args = parser.parse_args()

    out = repair_tenant_ledgers(
        scope=args.scope,
        target_tenant=args.target,
        dry_run=not args.apply,
        test=args.test_db,
    )
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())