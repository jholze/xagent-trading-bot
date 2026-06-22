#!/usr/bin/env bash
# Start monolith with architecture runtime (async notifications, optional Redis later).
# Rollback: ARCHITECTURE_MODE=monolith NOTIFICATION_MODE=direct bash scripts/start_with_ngrok.sh
set -euo pipefail
cd "$(dirname "$0")/.."
export ARCHITECTURE_MODE="${ARCHITECTURE_MODE:-monolith}"
export NOTIFICATION_MODE="${NOTIFICATION_MODE:-async}"
export TRADING_ENGINE_MODE="${TRADING_ENGINE_MODE:-in_process}"
exec bash scripts/start_with_ngrok.sh "$@"