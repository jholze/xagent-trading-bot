#!/usr/bin/env python3
"""Demo Mongo snapshot CLI — syncs demo JSON ledger via resolve_store.

Demo invariants (verification gating; do not violate):
  - Preserve ~25 open positions (~$100k equity, daily NAV delta < $2k)
  - Never overwrite Mongo positions/portfolio doc (read-only cache; full replace kept)
  - No manual test coins in demo orders (e.g. XRVM/USDT from unit/integration tests)
  - Orders/trade_history SOT is demo JSON; migrate_scope runs idempotently before sync
  - --dry-run calls migrate_scope(dry_run=True) then reads only (no Mongo/JSON writes)
  - --no-json apply syncs Mongo orders/trades only (no JSON file mutations)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.demo_snapshot_report import build_demo_snapshot_report, format_report_lines


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync demo ledger via resolve_store (stable by default)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test-db", action="store_true")
    parser.add_argument(
        "--from-live",
        action="store_true",
        help="Copy live ledger JSON into demo (destructive; default uses demo JSON)",
    )
    parser.add_argument(
        "--no-json",
        action="store_true",
        help="Read-only apply: sync Mongo only when not dry-run; skips JSON + migrate writes",
    )
    args = parser.parse_args()

    try:
        report = build_demo_snapshot_report(
            dry_run=args.dry_run,
            test_db=args.test_db,
            write_json=not args.no_json,
            from_live=args.from_live,
        )
        for line in format_report_lines(report):
            print(line)
        if report.get("invariant_violations"):
            return 2
    except Exception as exc:
        print(f"snapshot failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())