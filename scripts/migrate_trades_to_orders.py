#!/usr/bin/env python3
"""Migrate legacy trade_history files into orders.*.json ledgers (idempotent)."""

import json
import os
import sys
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data_manager import atomic_write_json, load_orders, save_orders

MIGRATIONS = [
    ("demo", "trade_history.demo.json", "paper"),
    ("paper", "trade_history.json", "paper"),
    ("live", "live_trade_history.json", "live"),
]


def _load_trades(path: str) -> list:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("trades", [])


def migrate_scope(scope: str, trade_path: str, default_mode: str) -> int:
    data = load_orders(scope)
    if data.get("migrated_from_trades"):
        existing = {o.get("legacy_trade_ts") for o in data.get("orders", []) if o.get("legacy_trade_ts")}
    else:
        existing = set()
        data["orders"] = []

    trades = _load_trades(trade_path)
    added = 0
    seq = max([int(o.get("display_seq", 0)) for o in data.get("orders", [])], default=0)

    for t in trades:
        ts = t.get("timestamp", "")
        if ts in existing:
            continue
        seq += 1
        side = (t.get("type") or "buy").lower()
        record = {
            "id": uuid.uuid4().hex[:12],
            "display_seq": seq,
            "status": "filled",
            "side": side,
            "symbol": t.get("symbol", ""),
            "timeframe": "4h",
            "order_type": "market",
            "source": t.get("source", "auto"),
            "signal": t.get("signal", ""),
            "trading_mode": t.get("mode", default_mode),
            "ledger_scope": scope,
            "legacy_trade_ts": ts,
            "request": {
                "price": float(t.get("price", 0)),
                "amount": float(t.get("amount", 0)),
                "usdt": float(t.get("usdt_amount", 0) or 0) or None,
            },
            "risk": {"approved": True, "message": "Migrated", "code": "", "size_multiplier": 1.0},
            "execution": {
                "price": float(t.get("price", 0)),
                "amount": float(t.get("amount", 0)),
                "usdt": float(t.get("usdt_amount") or t.get("usdt_received") or 0),
                "exchange_order_id": t.get("exchange_order_id"),
            },
            "pnl": t.get("pnl"),
            "error": None,
            "timestamps": {"created": ts or "", "updated": ts or "", "filled": ts or ""},
        }
        data.setdefault("orders", []).append(record)
        added += 1

    data["migrated_from_trades"] = True
    data["ledger_scope"] = scope
    save_orders(data, scope)
    return added


def main():
    total = 0
    for scope, path, mode in MIGRATIONS:
        n = migrate_scope(scope, path, mode)
        print(f"{scope}: +{n} orders from {path}")
        total += n
    print(f"Done. {total} orders migrated.")


if __name__ == "__main__":
    main()