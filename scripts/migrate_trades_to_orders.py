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


def _existing_legacy_timestamps(data: dict) -> set:
    if data.get("migrated_from_trades"):
        return {
            o.get("legacy_trade_ts")
            for o in data.get("orders", [])
            if o.get("legacy_trade_ts")
        }
    return set()


def preview_migrate_scope(scope: str, trade_path: str, default_mode: str) -> int:
    """Count trades that would be migrated without writing."""
    del default_mode  # signature parity with migrate_scope
    data = load_orders(scope)
    existing = _existing_legacy_timestamps(data)
    pending = 0
    for t in _load_trades(trade_path):
        if t.get("timestamp", "") not in existing:
            pending += 1
    return pending


def _trade_to_order_record(t: dict, *, scope: str, default_mode: str, seq: int) -> dict:
    ts = t.get("timestamp", "")
    side = (t.get("type") or "buy").lower()
    return {
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


def migrate_scope(
    scope: str,
    trade_path: str,
    default_mode: str,
    *,
    dry_run: bool = False,
) -> int:
    data = load_orders(scope)
    existing = _existing_legacy_timestamps(data)
    if not data.get("migrated_from_trades"):
        data["orders"] = []

    added = 0
    seq = max([int(o.get("display_seq", 0)) for o in data.get("orders", [])], default=0)

    for t in _load_trades(trade_path):
        ts = t.get("timestamp", "")
        if ts in existing:
            continue
        seq += 1
        data.setdefault("orders", []).append(
            _trade_to_order_record(t, scope=scope, default_mode=default_mode, seq=seq)
        )
        added += 1

    if dry_run:
        return added

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