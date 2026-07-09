#!/usr/bin/env python3
"""Generate prod vs staging comparison report under auswertungen/."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.stack_compare import (  # noqa: E402
    build_stack_compare_report,
    format_stack_compare_markdown,
)


def _remote_logs_dir() -> Path:
    return ROOT / "logs" / "remote"


def main() -> int:
    parser = argparse.ArgumentParser(description="Prod vs staging observability report")
    parser.add_argument("--hours", type=float, default=24.0, help="Lookback window")
    parser.add_argument("--out", default=None, help="Output markdown path")
    parser.add_argument("--remote-dir", default=None, help="Directory with pulled stack logs")
    args = parser.parse_args()

    until = datetime.now()
    since = until - timedelta(hours=args.hours)
    remote = Path(args.remote_dir) if args.remote_dir else _remote_logs_dir()

    staging_dec = None
    prod_dec = None
    staging_snap = None
    prod_snap = None
    if remote.exists():
        staging_dec = sorted(remote.glob("staging_decisions*.jsonl"))
        prod_dec = sorted(remote.glob("prod_decisions*.jsonl"))
        staging_snap = sorted(remote.glob("staging_snapshots*.jsonl"))
        prod_snap = sorted(remote.glob("prod_snapshots*.jsonl"))

    report = build_stack_compare_report(
        since=since,
        until=until,
        staging_decision_paths=staging_dec or None,
        prod_decision_paths=prod_dec or None,
        staging_snapshot_paths=staging_snap or None,
        prod_snapshot_paths=prod_snap or None,
    )
    md = format_stack_compare_markdown(report)
    out = Path(args.out) if args.out else ROOT / "auswertungen" / f"{until.strftime('%Y-%m-%d')}_stack_compare.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(md)
    print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())