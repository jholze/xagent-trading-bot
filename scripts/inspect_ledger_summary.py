#!/usr/bin/env python3
"""Print demo ledger summary (orders, cash, positions)."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("DEMO_MODE", "1")
os.environ.setdefault("DEMO_LEDGER_BACKEND", "mongo")
os.environ.setdefault("MONGODB_DB", "xagent_test")

from scripts.operator_mongo import prepare_operator_mongo
from data_manager import load_orders, load_trade_history, resolve_ledger_scope
from services.ledger_sync import count_open_positions_from_orders
from strategies.positions import bootstrap_positions, count_open_positions, list_active_positions


def main() -> int:
    meta = prepare_operator_mongo()
    if meta.get("pytest_isolated"):
        print("ERROR: resolved pytest-isolated DB — set MONGO_URL + DEMO_ALLOW_REMOTE_MONGO for Railway")
        return 1

    scope = resolve_ledger_scope()
    orders = load_orders(scope).get("orders", [])
    history = load_trade_history()
    cash = float(history.get("virtual_balance", 0) or 0)
    open_from_orders = count_open_positions_from_orders(scope)
    bootstrap_positions(scope=scope)
    active = list_active_positions()
    active_count = count_open_positions()

    print(f"db={meta['db']} host={meta['host']}")
    print(
        f"orders={len(orders)} cash={cash:.2f} "
        f"active_positions={active_count} (from_orders={open_from_orders})"
    )
    if orders:
        last = sorted(orders, key=lambda o: (o.get("timestamps") or {}).get("filled", ""))[-1]
        print(
            f"last_order={last.get('symbol')} {last.get('side')} "
            f"{(last.get('timestamps') or {}).get('filled', '')[:19]}"
        )
    for p in active[:8]:
        print(
            f"  {p['symbol']:12} amt={p.get('amount',0):.4f} "
            f"entry={p.get('average_entry',0):.4f}"
        )
    if len(active) > 8:
        print(f"  ... +{len(active)-8} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())