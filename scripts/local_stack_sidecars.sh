#!/usr/bin/env bash
# Start optional sidecars = Railway xagent-santiment + xagent-market-oracle (Tier C).
# Usage:
#   bash scripts/local_stack_sidecars.sh
#   bash scripts/local_stack_sidecars.sh --stop
set -euo pipefail
cd "$(dirname "$0")/.."

# shellcheck disable=SC1091
source scripts/local_stack_lib.sh
local_stack_load_env

if [[ "${1:-}" == "--stop" ]]; then
  local_stack_stop_pidfile santiment
  local_stack_stop_pidfile market-oracle
  echo "Sidecars stopped."
  exit 0
fi

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  echo "Usage: bash scripts/local_stack_sidecars.sh [--stop]"
  exit 0
fi

unset MONGO_URL
export DEMO_MODE=1
export MONGODB_DB="${MONGODB_DB:-xagent_test}"
export DRY_RUN="${DRY_RUN:-1}"
export BOT_INGEST_URL="${BOT_INGEST_URL:-http://127.0.0.1:${BOT_PORT:-5000}}"

run_dir="$(local_stack_run_dir)"

echo "=== Local stack sidecars (Tier C) ==="
echo "DRY_RUN=${DRY_RUN} BOT_INGEST_URL=${BOT_INGEST_URL}"

local_stack_stop_pidfile santiment
local_stack_stop_pidfile market-oracle

echo "Starting santiment on :${SANTIMENT_PORT}..."
PORT="${SANTIMENT_PORT}" nohup python3 -m services.santiment_sidecar \
  >"${run_dir}/santiment.log" 2>&1 &
local_stack_write_pid santiment $!
echo "  pid $(local_stack_read_pid santiment) log ${run_dir}/santiment.log"

echo "Starting market-oracle on :${MARKET_ORACLE_PORT}..."
PORT="${MARKET_ORACLE_PORT}" nohup python3 -m services.market_oracle \
  >"${run_dir}/market-oracle.log" 2>&1 &
local_stack_write_pid market-oracle $!
echo "  pid $(local_stack_read_pid market-oracle) log ${run_dir}/market-oracle.log"

sleep 1
soft_curl() {
  local name="$1" url="$2"
  if curl -sf -m 3 "$url" >/dev/null 2>&1; then
    echo "  OK ${name} ${url}"
  else
    echo "  WAIT ${name} ${url} (check log if still down)"
  fi
}
soft_curl santiment "http://127.0.0.1:${SANTIMENT_PORT}/health"
soft_curl market-oracle "http://127.0.0.1:${MARKET_ORACLE_PORT}/health"

echo "Stop: bash scripts/local_stack_sidecars.sh --stop"
