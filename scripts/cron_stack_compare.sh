#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
LOG="${BOT_DIR:-$(pwd)}/logs/stack_compare_cron.log"
{
  echo "=== $(date -Iseconds) stack compare cron ==="
  bash scripts/pull_stack_observability.sh
  python3 scripts/stack_compare_report.py --hours 24
} >>"$LOG" 2>&1