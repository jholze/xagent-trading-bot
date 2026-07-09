#!/usr/bin/env python3
"""Ledger replay summary for rule-set impact (portfolio-wide)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEMO_MODE", "1")

from data_manager import load_orders  # noqa: E402
from hermes.sell_rotation_replay import compare_policies, format_report, order_filled_ts, parse_ts  # noqa: E402
from services.config_fingerprint import config_fingerprint, extract_rule_snapshot  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare sell rule impact on ledger window")
    parser.add_argument("--scope", default="demo", help="Ledger scope")
    parser.add_argument("--since", required=True, help="ISO date — orders on/after")
    parser.add_argument("--out", default=None, help="JSON output path")
    parser.add_argument("--label", default="current", help="Rule-set label for report")
    args = parser.parse_args()

    since = parse_ts(args.since)
    orders = [
        o for o in load_orders(args.scope).get("orders", [])
        if (ts := order_filled_ts(o)) and ts >= since
    ]
    decisions_path = ROOT / "logs" / "decisions.jsonl"
    if not decisions_path.exists():
        decisions_path = None

    report = compare_policies(
        orders,
        decisions_path=decisions_path,
        since=since,
    )

    from core.config import get_bot_config

    cfg = get_bot_config().raw
    payload = {
        "label": args.label,
        "since": since.isoformat(),
        "scope": args.scope,
        "generated_at": datetime.now().isoformat(),
        "config_fingerprint": config_fingerprint(cfg),
        "rule_snapshot": extract_rule_snapshot(cfg),
        "baseline": {
            "filled_orders": report["baseline"].filled_orders,
            "partial_sell_share": report["baseline"].partial_sell_share,
            "closed_cycles": report["baseline"].closed_cycles,
            "zombie_tails": report["baseline"].zombie_tails,
        },
        "open_count": len(report.get("open_cycles") or []),
        "forward_open_D_prime": {
            "would_close_now": report["forward_open"]["D_prime"].would_close_now,
            "free_slots": report["forward_open"]["D_prime"].free_slots,
        },
    }

    text = format_report(report, max_open_slots=50)
    print(text)
    print(f"\nconfig_fingerprint={payload['config_fingerprint']} label={args.label}")

    if args.out:
        Path(args.out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())