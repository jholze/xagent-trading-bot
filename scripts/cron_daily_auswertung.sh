#!/usr/bin/env bash
# Cron wrapper: daily bot report → auswertungen/YYYY-MM-DD_tag.md
set -euo pipefail

BOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$BOT_DIR"

export PATH="/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:/Library/Frameworks/Python.framework/Versions/3.13/bin:${PATH:-}"

if [[ -f "$BOT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$BOT_DIR/.env"
  set +a
fi

LOG_DIR="$BOT_DIR/logs"
LOG_FILE="$LOG_DIR/daily_auswertung_cron.log"
mkdir -p "$LOG_DIR"

{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') daily_auswertung start ==="
  python3 scripts/daily_auswertung.py --telegram
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') daily_auswertung ok ==="
} >> "$LOG_FILE" 2>&1