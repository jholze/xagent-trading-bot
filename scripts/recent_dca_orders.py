#!/usr/bin/env python3
"""List recent DCA / DCA-recovery buy sizes."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("DEMO_MODE", "1")
os.environ.setdefault("DEMO_LEDGER_BACKEND", "mongo")
os.environ.setdefault("MONGODB_DB", "xagent_test")

from data_manager import load_orders, resolve_ledger_scope


def main() -> int:
    scope = resolve_ledger_scope()
    orders = [
        o
        for o in load_orders(scope).get("orders", [])
        if o.get("status") == "filled"
        and o.get("side") == "buy"
        and o.get("source") in ("dca", "dca_recovery")
    ]
    orders.sort(key=lambda o: str((o.get("timestamps") or {}).get("filled", "")), reverse=True)
    orders = orders[:25]
    print(f"Recent DCA orders ({len(orders)}):")
    amounts = []
    for o in orders:
        usdt = float((o.get("execution") or {}).get("usdt") or (o.get("request") or {}).get("usdt") or 0)
        amounts.append(usdt)
        ts = str((o.get("timestamps") or {}).get("filled", ""))[:16]
        print(f"  {ts} {o.get('symbol', '?'):14} {o.get('source', '?'):14} ${usdt:.2f}")
    if amounts:
        uniq = sorted(set(round(a, 2) for a in amounts))
        print(f"\nUnique sizes: {uniq}")
        print(f"Avg: ${sum(amounts)/len(amounts):.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())