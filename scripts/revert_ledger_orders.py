#!/usr/bin/env python3
"""Remove filled orders from ledger and rebuild positions (demo/test only)."""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("DEMO_MODE", "1")


def main() -> int:
    parser = argparse.ArgumentParser(description="Revert specific filled orders from ledger")
    parser.add_argument("--scope", default="demo")
    parser.add_argument("--order-id", action="append", dest="order_ids", default=[])
    parser.add_argument("--display-seq", type=int, action="append", dest="display_seqs", default=[])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    from data_manager import (
        load_orders,
        load_trade_history_document,
        save_orders,
        save_trade_history_document,
    )
    from services.ledger_sync import rebuild_positions_from_orders

    if not args.order_ids and not args.display_seqs:
        print("Provide --order-id and/or --display-seq")
        return 1

    data = load_orders(args.scope)
    orders = list(data.get("orders", []))
    remove = set(args.order_ids)
    for seq in args.display_seqs:
        for o in orders:
            if int(o.get("display_seq", -1)) == int(seq):
                remove.add(o.get("id"))
    kept = [o for o in orders if o.get("id") not in remove]
    removed = [o for o in orders if o.get("id") in remove]

    if not removed:
        print("No matching orders found.")
        return 1

    print(f"Scope: {args.scope}")
    for o in removed:
        ts = (o.get("timestamps") or {}).get("filled", "?")[:19]
        print(
            f"  remove {o.get('id')} {o.get('side')} {o.get('symbol')} "
            f"{o.get('signal')} pnl={o.get('pnl')} @ {ts}"
        )

    if args.dry_run:
        print("Dry-run — no writes.")
        return 0

    data["orders"] = kept
    save_orders(data, args.scope)

    history = load_trade_history_document(args.scope)
    trades = list(history.get("trades", []))
    removed_ids = remove
    new_trades = [t for t in trades if t.get("order_id") not in removed_ids]
    if len(new_trades) != len(trades):
        history["trades"] = new_trades
        from data_manager import compute_sim_realized_pnl

        history["realized_pnl"] = compute_sim_realized_pnl(new_trades)
        history["total_pnl"] = history["realized_pnl"]
        save_trade_history_document(history, args.scope)
        print(f"Trade history: removed {len(trades) - len(new_trades)} trade(s)")

    open_count = rebuild_positions_from_orders(args.scope)
    print(f"Rebuilt positions: {open_count} open")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())