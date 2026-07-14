#!/usr/bin/env python3
"""Summarize per-tenant Mongo ledgers (local or Railway via MONGO_URL)."""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from core.tenant_context import DEFAULT_TENANT
from storage.mongo_client import close_client, mongo_uri_host, ping_database, resolve_database_name
from storage.mongo_ledger import ORDERS_COLLECTION, POSITIONS_COLLECTION, TRADE_HISTORY_COLLECTION
from storage.tenant_keys import compound_ledger_id
from strategies.positions import is_open_position


def _summarize(db, coll: str, doc_id: str, payload_key: str) -> str:
    doc = db[coll].find_one({"_id": doc_id})
    if not doc:
        return "MISSING"
    if payload_key == "positions":
        pos = doc.get("positions") or {}
        open_n = sum(1 for p in pos.values() if is_open_position(p))
        return f"{len(pos)} keys, {open_n} open, tenant={doc.get('tenant_id', '-')}"
    key = "orders" if coll == ORDERS_COLLECTION else "trades"
    entries = doc.get(key) or []
    if coll == ORDERS_COLLECTION:
        filled = sum(1 for o in entries if o.get("status") == "filled")
        return f"{len(entries)} orders ({filled} filled), tenant={doc.get('tenant_id', '-')}"
    cash = doc.get("virtual_balance")
    cash_s = f", cash=${float(cash or 0):,.2f}" if cash is not None else ""
    return f"{len(entries)} trades{cash_s}, tenant={doc.get('tenant_id', '-')}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", default=None, help="demo|paper (default: resolve_ledger_scope())")
    parser.add_argument("--tenant", action="append", default=[], help="Tenant id (repeatable)")
    args = parser.parse_args()

    os.environ.setdefault("MULTI_TENANT_ENABLED", "1")
    if not ping_database():
        print("ERROR: Mongo ping failed", file=sys.stderr)
        return 1

    from data_manager import resolve_ledger_scope

    scope = args.scope or resolve_ledger_scope()
    tenants = args.tenant or [DEFAULT_TENANT, "henry"]
    db_name = resolve_database_name()
    from storage.mongo_client import get_client

    client = get_client()
    db = client[db_name]
    print(f"host={mongo_uri_host(os.environ.get('MONGO_URL') or os.environ.get('MONGODB_URI', ''))}")
    print(f"db={db_name} scope={scope}\n")

    doc_ids = [("legacy", scope)]
    for tid in tenants:
        doc_ids.append((tid, compound_ledger_id(tid, scope)))

    for label, doc_id in doc_ids:
        print(f"--- {label} (_id={doc_id}) ---")
        for coll, key in (
            (ORDERS_COLLECTION, "orders"),
            (POSITIONS_COLLECTION, "positions"),
            (TRADE_HISTORY_COLLECTION, "trades"),
        ):
            print(f"  {coll}: {_summarize(db, coll, doc_id, key)}")
        print()

    from storage.mongo_ledger import MongoLedgerStore

    store = MongoLedgerStore()
    print("--- effective read (MongoLedgerStore) ---")
    for tid in tenants:
        o = store.load_orders(scope, tenant_id=tid)
        p = store.load_positions(scope, tenant_id=tid)
        h = store.load_trade_history(scope, tenant_id=tid)
        open_n = sum(1 for x in (p.get("positions") or {}).values() if is_open_position(x))
        print(
            f"  {tid}: orders={len(o.get('orders', []))} "
            f"positions={len(p.get('positions', {}))} open={open_n} "
            f"trades={len(h.get('trades', []))} cash=${float(h.get('virtual_balance', 0) or 0):,.2f}"
        )

    close_client()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())