#!/usr/bin/env bash
# Start memory cortex UI = Railway xagent-memory-cortex.
# Usage:
#   bash scripts/local_stack_cortex.sh
#   bash scripts/local_stack_cortex.sh --bg
#   bash scripts/local_stack_cortex.sh --stop
set -euo pipefail
cd "$(dirname "$0")/.."

# shellcheck disable=SC1091
source scripts/local_stack_lib.sh
local_stack_load_env

if [[ "${1:-}" == "--stop" ]]; then
  local_stack_stop_pidfile cortex
  exit 0
fi

export PORT="${MEMORY_CORTEX_PORT:-8765}"
export MEMORY_VIZ_DEMO="${MEMORY_VIZ_DEMO:-0}"
export MONGODB_DB="${MONGODB_DB:-xagent_test}"
unset MONGO_URL
# memory_viz may use MONGO_URL or MONGODB_URI depending on module
export MONGODB_URI="${MONGODB_URI:-mongodb://127.0.0.1:27017}"

echo "=== Local stack cortex (xagent-memory-cortex) ==="
echo "PORT=${PORT} MEMORY_VIZ_DEMO=${MEMORY_VIZ_DEMO}"

if [[ "${1:-}" == "--bg" ]]; then
  local_stack_stop_pidfile cortex
  run_dir="$(local_stack_run_dir)"
  nohup python3 -m tools.memory_viz.server \
    >"${run_dir}/cortex.log" 2>&1 &
  local_stack_write_pid cortex $!
  echo "Cortex pid $(local_stack_read_pid cortex) — http://127.0.0.1:${PORT}/"
  exit 0
fi

exec python3 -m tools.memory_viz.server
