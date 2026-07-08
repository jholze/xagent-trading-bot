#!/usr/bin/env python3
"""Reset demo Mongo ledger to a clean cash balance (no orders/positions)."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SCOPE = "demo"


def _ensure_demo_mongo() -> None:
    os.environ.setdefault("DEMO_MODE", "1")
    os.environ.setdefault("DEMO_LEDGER_BACKEND", "mongo")


def fresh_bundle(balance_usdt: float) -> dict:
    return {
        "scope": SCOPE,
        "orders": {"ledger_scope": SCOPE, "orders": [], "migrated_from_trades": False},
        "positions": {"ledger_scope": SCOPE, "positions": {}},
        "trade_history": {
            "virtual_balance": float(balance_usdt),
            "realized_pnl": 0.0,
            "open_positions": 0,
            "trades": [],
        },
    }


def reset_demo_ledger(balance_usdt: float) -> dict:
    _ensure_demo_mongo()
    from data_manager import get_config, _mongo_ledger_store
    from storage.mongo_client import assert_safe_demo_mongo_db
    from services.ledger_sync import sync_positions_on_startup
    from strategies.positions import bootstrap_positions, flush_positions

    db = assert_safe_demo_mongo_db()
    payload = fresh_bundle(balance_usdt)
    store = _mongo_ledger_store(get_config())
    store.save_orders(payload["orders"], SCOPE)
    store.save_trade_history(payload["trade_history"], SCOPE)
    store.save_positions(payload["positions"], SCOPE)
    bootstrap_positions(scope=SCOPE)
    flush_positions(scope=SCOPE, force=True)
    sync_positions_on_startup()
    from data_manager import reconcile_demo_trade_history_on_startup

    reconcile_demo_trade_history_on_startup()
    return {
        "database": db,
        "orders": 0,
        "positions": 0,
        "virtual_balance": float(balance_usdt),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset demo ledger to empty $100k-style start")
    parser.add_argument(
        "--balance",
        type=float,
        default=100_000.0,
        help="Starting virtual_balance USDT (default: 100000)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt",
    )
    args = parser.parse_args()

    if not args.yes:
        from data_manager import get_config, _mongo_ledger_store

        _ensure_demo_mongo()
        store = _mongo_ledger_store(get_config())
        orders = len((store.load_orders(SCOPE).get("orders") or []))
        positions = len((store.load_positions(SCOPE).get("positions") or {}))
        cash = store.load_trade_history(SCOPE).get("virtual_balance")
        print(f"Current demo ledger: orders={orders} positions={positions} cash={cash}")
        print(f"Will reset to: orders=0 positions=0 cash={args.balance:,.0f}")
        if input("Type RESET to continue: ").strip() != "RESET":
            print("Aborted.")
            return 1

    stats = reset_demo_ledger(args.balance)
    print(
        f"[reset] db={stats.get('database')} "
        f"orders={stats['orders']} positions={stats['positions']} "
        f"cash={stats['virtual_balance']:,.2f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())