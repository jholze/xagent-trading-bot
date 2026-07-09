#!/usr/bin/env python3
"""Demo Mongo snapshot CLI — reads demo ledger from Mongo (default).

Demo invariants (verification gating; do not violate):
  - Preserve ~25 open positions (~$100k equity, daily NAV delta < $2k)
  - No manual test coins in demo orders (e.g. XRVM/USDT from unit/integration tests)
  - Mongo is SOT; --write-json exports *.demo.json for backup only
  - --dry-run reads only (no Mongo/JSON writes)
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
        description="Demo ledger snapshot from Mongo (JSON export opt-in)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test-db", action="store_true")
    parser.add_argument(
        "--from-live",
        action="store_true",
        help="Copy live ledger JSON into demo (destructive; default reads demo Mongo)",
    )
    parser.add_argument(
        "--write-json",
        action="store_true",
        help="Also write *.demo.json backup files (not used by runtime)",
    )
    parser.add_argument(
        "--no-json",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    try:
        report = build_demo_snapshot_report(
            dry_run=args.dry_run,
            test_db=args.test_db,
            write_json=args.write_json and not args.no_json,
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