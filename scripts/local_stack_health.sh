#!/usr/bin/env bash
# Health check for local Railway-parity stack.
# Usage:
#   bash scripts/local_stack_health.sh
#   bash scripts/local_stack_health.sh --infra
set -euo pipefail
cd "$(dirname "$0")/.."

# shellcheck disable=SC1091
source scripts/local_stack_lib.sh
local_stack_load_env

INFRA_ONLY=0
[[ "${1:-}" == "--infra" ]] && INFRA_ONLY=1

ok=0
fail=0
skip=0

check() {
  local label="$1"
  local cmd="$2"
  if eval "$cmd" >/dev/null 2>&1; then
    echo "  OK   ${label}"
    ok=$((ok + 1))
  else
    echo "  FAIL ${label}"
    fail=$((fail + 1))
  fi
}

soft() {
  local label="$1"
  local cmd="$2"
  if eval "$cmd" >/dev/null 2>&1; then
    echo "  OK   ${label}"
    ok=$((ok + 1))
  else
    echo "  --   ${label} (optional / not running)"
    skip=$((skip + 1))
  fi
}

echo "=== Local stack health ==="
echo "Map: Railway service → local"
echo ""

check "Mongo (plugin) @ ${MONGODB_URI} / ${MONGODB_DB}" \
  "local_stack_mongo_ok"

check "Redis (plugin) @ ${REDIS_URL}" \
  "redis-cli -u '${REDIS_URL}' ping | grep -qi pong"

soft "Weaviate (xagent-weaviate) @ ${WEAVIATE_URL}" \
  "curl -sf -m 3 '${WEAVIATE_URL}/v1/.well-known/ready'"

if [[ "$INFRA_ONLY" == "1" ]]; then
  echo ""
  echo "Infra only: ok=${ok} fail=${fail} optional_down=${skip}"
  [[ "$fail" -eq 0 ]]
  exit $?
fi

soft "Bot (xagent-test) :${BOT_PORT}/health" \
  "curl -sf -m 3 'http://127.0.0.1:${BOT_PORT}/health'"

soft "Hermes (xagent-hermes) :${HERMES_PORT}/health" \
  "curl -sf -m 3 'http://127.0.0.1:${HERMES_PORT}/health'"

soft "Santiment (xagent-santiment) :${SANTIMENT_PORT}/health" \
  "curl -sf -m 3 'http://127.0.0.1:${SANTIMENT_PORT}/health'"

soft "Market oracle (xagent-market-oracle) :${MARKET_ORACLE_PORT}/health" \
  "curl -sf -m 3 'http://127.0.0.1:${MARKET_ORACLE_PORT}/health'"

soft "Memory cortex (xagent-memory-cortex) :${MEMORY_CORTEX_PORT}" \
  "curl -sf -m 3 'http://127.0.0.1:${MEMORY_CORTEX_PORT}/' || curl -sf -m 3 'http://127.0.0.1:${MEMORY_CORTEX_PORT}/health'"

echo ""
echo "Summary: ok=${ok} fail=${fail} optional_down=${skip}"
echo ""
echo "Railway ↔ local ports (defaults):"
echo "  xagent-test           → :${BOT_PORT}   (ngrok for Telegram)"
echo "  xagent-hermes         → :${HERMES_PORT}"
echo "  xagent-weaviate       → :8080"
echo "  xagent-santiment      → :${SANTIMENT_PORT}"
echo "  xagent-market-oracle  → :${MARKET_ORACLE_PORT}"
echo "  xagent-memory-cortex  → :${MEMORY_CORTEX_PORT}"
echo "  Mongo / Redis         → :27017 / :6379"

# Infra hard requirements only for exit code when full check
if [[ "$fail" -gt 0 ]]; then
  exit 1
fi
exit 0
