#!/usr/bin/env python3
"""Migrate legacy scope-only Mongo ledger docs to tenant-scoped compound keys."""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.tenant_context import DEFAULT_TENANT
from storage.mongo_client import get_database
from storage.tenant_keys import compound_ledger_id, is_legacy_doc
from storage.tenant_registry import ensure_default_tenant

COLLECTIONS = ("orders", "positions", "trade_history")
SCOPES = ("demo", "paper", "live")


def migrate(*, test: bool = False, dry_run: bool = False) -> dict:
    ensure_default_tenant(test=test)
    db = get_database(test=test)
    stats: dict = {"migrated": 0, "skipped": 0, "scopes": []}

    for coll_name in COLLECTIONS:
        coll = db[coll_name]
        for scope in SCOPES:
            legacy = coll.find_one({"_id": scope})
            if not legacy or not is_legacy_doc(legacy):
                stats["skipped"] += 1
                continue
            compound_id = compound_ledger_id(DEFAULT_TENANT, scope)
            existing = coll.find_one({"_id": compound_id})
            payload = {k: v for k, v in legacy.items() if k != "_id"}
            payload["tenant_id"] = DEFAULT_TENANT
            payload["ledger_scope"] = scope
            payload["_id"] = compound_id
            label = f"{coll_name}:{compound_id}"
            if existing:
                stats["skipped"] += 1
                continue
            if dry_run:
                stats["migrated"] += 1
                stats["scopes"].append(label)
                continue
            coll.replace_one({"_id": compound_id}, payload, upsert=True)
            stats["migrated"] += 1
            stats["scopes"].append(label)

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report without writing")
    parser.add_argument("--test", action="store_true", help="Use xagent_test database")
    args = parser.parse_args()
    if args.test:
        os.environ["MONGODB_DB"] = "xagent_test"
    stats = migrate(test=args.test, dry_run=args.dry_run)
    mode = "dry-run" if args.dry_run else "apply"
    print(f"migrate_single_to_tenant [{mode}] tenant={DEFAULT_TENANT}")
    print(f"migrated={stats['migrated']} skipped={stats['skipped']}")
    for label in stats["scopes"]:
        print(f"  {label}")
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())