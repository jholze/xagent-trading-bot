#!/usr/bin/env python3
"""Analyze demo-ledger sell patterns and compare rotation policies A–D.

Usage:
  python3 scripts/analyze_sell_rotation.py
  python3 scripts/analyze_sell_rotation.py --scope demo --json
  python3 scripts/analyze_sell_rotation.py --since 2026-06-25
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEMO_MODE", "1")

from data_manager import load_orders  # noqa: E402
from hermes.sell_rotation_replay import (  # noqa: E402
    compare_policies,
    format_report,
    order_filled_ts,
    parse_ts,
)


def filter_orders_since(orders: list[dict], since: datetime | None) -> list[dict]:
    if not since:
        return orders
    out = []
    for o in orders:
        ts = order_filled_ts(o)
        if ts and ts >= since:
            out.append(o)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Sell rotation ledger analysis (policies A–D)")
    parser.add_argument("--scope", default="demo", help="Ledger scope (default: demo)")
    parser.add_argument("--since", default=None, help="ISO date — only orders on/after this date")
    parser.add_argument("--max-open", type=int, default=40, help="max_open_positions for free-slot calc")
    parser.add_argument("--decisions", default=None, help="Path to decisions.jsonl (default: logs/decisions.jsonl)")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text report")
    parser.add_argument("--validate", action="store_true", help="Print Gate 1 Go/No-Go summary")
    parser.add_argument(
        "--baseline-out",
        default=None,
        help="Write full JSON snapshot to path (implies structured output)",
    )
    args = parser.parse_args()

    since = parse_ts(args.since) if args.since else None
    orders = load_orders(args.scope).get("orders", [])
    orders = filter_orders_since(orders, since)

    decisions_path = Path(args.decisions) if args.decisions else ROOT / "logs" / "decisions.jsonl"
    if not decisions_path.exists():
        decisions_path = None

    report = compare_policies(
        orders,
        decisions_path=decisions_path,
        max_open_slots=args.max_open,
        since=since,
    )

    def _build_payload() -> dict:
        recovery = report["recovery"]
        tail_slots = report["tail_slots"]
        return {
            "baseline": {
                "filled_orders": report["baseline"].filled_orders,
                "partial_sell_share": report["baseline"].partial_sell_share,
                "closed_cycles": report["baseline"].closed_cycles,
                "open_cycles": report["baseline"].open_cycles,
                "zombie_tails": report["baseline"].zombie_tails,
            },
            "forward_open": {
                k: {
                    "would_close_now": v.would_close_now,
                    "would_close_losers": v.would_close_losers,
                    "tail_exempt": v.tail_exempt,
                    "full_slots": v.full_slots,
                    "free_slots": v.free_slots,
                }
                for k, v in report["forward_open"].items()
            },
            "open_policies": {
                k: {
                    "label": v.label,
                    "executed_sells": v.executed_sells,
                    "blocked_sells": v.blocked_sells,
                    "full_close_conversions": v.full_close_conversions,
                    "tail_auto_closes": v.tail_auto_closes,
                    "cycles_closed": v.cycles_closed,
                    "open_cycles": v.open_cycles,
                    "tail_cycles": v.tail_cycles,
                    "effective_open_slots": v.effective_open_slots,
                    "free_slots": max(0, args.max_open - int(v.effective_open_slots)),
                    "realized_pnl": v.realized_pnl,
                }
                for k, v in report["open_policies"].items()
            },
            "D_prime": {
                "forward": report["forward_open"]["D_prime"].__dict__,
                "open_policy": report["open_policies"]["D_prime"].__dict__,
            },
            "recovery": {
                "eligible": recovery.eligible,
                "blocked": recovery.blocked,
                "minus_tails": recovery.minus_tails,
                "details": recovery.details,
            },
            "tail_slots": {
                "open_total": tail_slots.open_total,
                "open_full_slots": tail_slots.open_full_slots,
                "open_tail_exempt": tail_slots.open_tail_exempt,
                "free_buy_slots": tail_slots.free_buy_slots,
            },
            "validation": report.get("validation"),
        }

    if args.baseline_out:
        out_path = Path(args.baseline_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(_build_payload(), indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(_build_payload(), indent=2))
    else:
        print(format_report(report, max_open_slots=args.max_open))

    if args.validate:
        validation = report.get("validation") or {}
        verdict = "GO" if validation.get("go") else "NO-GO"
        print(f"\nGATE 1: {verdict}")
        for name, gate in (validation.get("gates") or {}).items():
            status = "PASS" if gate.get("pass") else "FAIL"
            print(f"  [{status}] {name}: {gate.get('value')} (need {gate.get('threshold')})")

        # Stuck tails detail
        stuck = []
        for c in report["cycles"]:
            if c.close_ts or c.amount <= 0:
                continue
            if c.peak_amount <= 0:
                continue
            sold = 1.0 - c.amount / c.peak_amount
            if sold < 0.20:
                continue
            last_sell = c.sells[-1].ts if c.sells else None
            idle = (datetime.now() - last_sell).total_seconds() / 86400 if last_sell else 0
            stuck.append((c.symbol, c.timeframe, sold, len(c.sells), idle))
        if stuck:
            print("\nSTUCK TAILS (open, >=20% sold):")
            for sym, tf, sold, n_sells, idle in sorted(stuck, key=lambda x: -x[4])[:15]:
                print(f"  {sym:14s} {tf:3s}  sold={sold*100:4.0f}%  sells={n_sells:2d}  idle={idle:.1f}d")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())