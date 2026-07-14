#!/usr/bin/env python3
"""Move orders from one tenant ledger to another (e.g. default → henry after MT fix)."""

from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage.mongo_ledger import MongoLedgerStore
from storage.tenant_keys import compound_ledger_id


def migrate_orders(
    *,
    scope: str,
    source_tenant: str,
    target_tenant: str,
    dry_run: bool = True,
    test: bool = False,
) -> dict:
    store = MongoLedgerStore(test=test)
    src = store.load_orders(scope, tenant_id=source_tenant)
    tgt = store.load_orders(scope, tenant_id=target_tenant)
    src_orders = list(src.get("orders") or [])
    tgt_orders = list(tgt.get("orders") or [])
    if not src_orders:
        return {"moved": 0, "message": f"No orders under {source_tenant}:{scope}"}

    merged = copy.deepcopy(tgt_orders)
    existing_ids = {o.get("id") for o in merged if o.get("id")}
    moved = 0
    for order in src_orders:
        oid = order.get("id")
        if oid and oid in existing_ids:
            continue
        merged.append(copy.deepcopy(order))
        moved += 1

    result = {
        "moved": moved,
        "source": compound_ledger_id(source_tenant, scope),
        "target": compound_ledger_id(target_tenant, scope),
        "source_before": len(src_orders),
        "target_before": len(tgt_orders),
        "target_after": len(merged),
        "dry_run": dry_run,
    }
    if dry_run or moved == 0:
        return result

    tgt_payload = dict(tgt)
    tgt_payload["orders"] = merged
    store.save_orders(tgt_payload, scope, tenant_id=target_tenant)

    src_payload = dict(src)
    src_payload["orders"] = []
    store.save_orders(src_payload, scope, tenant_id=source_tenant)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate orders between tenant ledgers")
    parser.add_argument("--scope", default="paper")
    parser.add_argument("--from", dest="source", default="default")
    parser.add_argument("--to", dest="target", required=True)
    parser.add_argument("--apply", action="store_true", help="Persist changes (default: dry-run)")
    parser.add_argument("--test-db", action="store_true", help="Use pytest Mongo DB")
    args = parser.parse_args()

    out = migrate_orders(
        scope=args.scope,
        source_tenant=args.source,
        target_tenant=args.target,
        dry_run=not args.apply,
        test=args.test_db,
    )
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())