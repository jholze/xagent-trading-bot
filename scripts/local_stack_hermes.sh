#!/usr/bin/env bash
# Start local Hermes + Trading Memory = Railway xagent-hermes.
# Does NOT write orders/positions.
# Usage:
#   bash scripts/local_stack_hermes.sh
#   bash scripts/local_stack_hermes.sh --fg     # foreground (default)
#   bash scripts/local_stack_hermes.sh --bg
set -euo pipefail
cd "$(dirname "$0")/.."

# shellcheck disable=SC1091
source scripts/local_stack_lib.sh
local_stack_load_env

BG=0
for arg in "$@"; do
  case "$arg" in
    --bg) BG=1 ;;
    --fg) BG=0 ;;
    -h|--help)
      echo "Usage: bash scripts/local_stack_hermes.sh [--bg|--fg]"
      exit 0
      ;;
  esac
done

export DEMO_MODE=1
export MONGODB_DB="${MONGODB_DB:-xagent_test}"
export PORT="${HERMES_PORT:-8090}"
export WEAVIATE_URL="${WEAVIATE_URL:-http://127.0.0.1:8080}"
export HERMES_RUN_LEARNING="${HERMES_RUN_LEARNING:-0}"
export RUN_HERMES=1
# Never inherit Railway mongo
unset MONGO_URL

echo "=== Local stack hermes (xagent-hermes) ==="
echo "PORT=${PORT} WEAVIATE_URL=${WEAVIATE_URL} HERMES_RUN_LEARNING=${HERMES_RUN_LEARNING}"
echo "Mongo ${MONGODB_URI} / ${MONGODB_DB}"

if [[ "$BG" == "1" ]]; then
  local_stack_stop_pidfile hermes
  mkdir -p "$(local_stack_run_dir)"
  nohup python3 -m intelligence.memory.service \
    >"$(local_stack_run_dir)/hermes.log" 2>&1 &
  pid=$!
  local_stack_write_pid hermes "$pid"
  echo "Hermes pid ${pid} — log: run/local_stack/hermes.log"
  echo "Health: curl -s http://127.0.0.1:${PORT}/health"
  exit 0
fi

exec python3 -m intelligence.memory.service
