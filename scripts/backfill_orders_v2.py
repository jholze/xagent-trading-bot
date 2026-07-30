#!/usr/bin/env python3
"""Backfill legacy orders blob → orders_v2 (per-order docs).

Usage (Railway / operator Mongo):
  PYTHONPATH=/app python3 scripts/backfill_orders_v2.py
  PYTHONPATH=/app python3 scripts/backfill_orders_v2.py --tenants default,henry --dry-run

After a successful full backfill set env:
  ORDER_LEDGER_V2_READS=1
  ORDER_LEDGER_V2_BACKFILL_COMPLETE=1
"""

from __future__ import annotations

import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("DEMO_MODE", "1")
os.environ.setdefault("DEMO_LEDGER_BACKEND", "mongo")
os.environ.setdefault("DEMO_ALLOW_REMOTE_MONGO", "1")


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill orders blob → orders_v2")
    parser.add_argument("--tenants", default="default,henry", help="Comma-separated tenant ids")
    parser.add_argument("--scope", default="demo", help="Ledger scope (default demo)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-log", type=int, default=50)
    args = parser.parse_args()

    from scripts.operator_mongo import prepare_operator_mongo
    from core.tenant_context import tenant_context
    from data_manager import load_orders
    from storage.order_ledger_v2 import (
        get_order_ledger_v2,
        reset_order_ledger_v2_for_tests,
    )

    meta = prepare_operator_mongo()
    print(f"mongo db={meta.get('db')} host={meta.get('host')} dry_run={args.dry_run}")

    # Force mongo v2 store
    os.environ["ORDER_LEDGER_V2"] = "1"
    os.environ["ORDER_LEDGER_V2_BACKEND"] = "mongo"
    reset_order_ledger_v2_for_tests()
    store = get_order_ledger_v2()
    if store is None:
        print("ERROR: order ledger v2 store unavailable")
        return 1
    try:
        store.ensure_indexes()
    except Exception as e:
        print(f"ensure_indexes warn: {e}")

    tenants = [t.strip() for t in args.tenants.split(",") if t.strip()]
    scope = args.scope
    total_up = 0
    total_skip = 0

    for tid in tenants:
        with tenant_context(tid):
            t0 = time.time()
            blob = load_orders(scope).get("orders") or []
            print(f"\n=== tenant={tid} scope={scope} blob={len(blob)} ===")
            up = 0
            for i, o in enumerate(blob):
                if not o.get("id"):
                    total_skip += 1
                    continue
                if args.dry_run:
                    up += 1
                else:
                    try:
                        store.upsert_order(o)
                        up += 1
                    except Exception as e:
                        print(f"  upsert fail id={o.get('id')}: {e}")
                if args.batch_log and (i + 1) % args.batch_log == 0:
                    print(f"  … {i + 1}/{len(blob)}")
            dt = time.time() - t0
            print(f"  upserted={up} elapsed={dt:.1f}s")
            total_up += up

    print(f"\nDONE upserted={total_up} skipped={total_skip} dry_run={args.dry_run}")
    print("Next: set ORDER_LEDGER_V2_READS=1 ORDER_LEDGER_V2_BACKFILL_COMPLETE=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
