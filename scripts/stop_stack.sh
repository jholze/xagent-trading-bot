#!/usr/bin/env bash
# Stop monolith + optional worker pidfiles (Phase 0 rollback helper).
set -euo pipefail
cd "$(dirname "$0")/.."
bash scripts/stop_bot.sh 2>/dev/null || true
for f in run/hermes.pid run/background.pid run/notification.pid; do
  if [[ -f "$f" ]]; then
    pid="$(cat "$f" 2>/dev/null || true)"
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
    rm -f "$f"
  fi
done
echo "Stack stopped."