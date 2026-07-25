#!/usr/bin/env bash
# Start local infra for Railway-parity stack (Tier A/B base).
# Usage:
#   bash scripts/local_stack_up.sh           # host mongo/redis + docker weaviate
#   bash scripts/local_stack_up.sh --full    # docker mongo+redis+weaviate
#   bash scripts/local_stack_up.sh --no-weaviate
set -euo pipefail
cd "$(dirname "$0")/.."

# shellcheck disable=SC1091
source scripts/local_stack_lib.sh
local_stack_load_env

FULL="${LOCAL_STACK_FULL:-0}"
WITH_WEAVIATE=1
for arg in "$@"; do
  case "$arg" in
    --full) FULL=1 ;;
    --no-weaviate) WITH_WEAVIATE=0 ;;
    -h|--help)
      echo "Usage: bash scripts/local_stack_up.sh [--full] [--no-weaviate]"
      exit 0
      ;;
  esac
done

echo "=== Local stack up (Railway parity infra) ==="
echo "Mongo: ${MONGODB_URI} db=${MONGODB_DB}"
echo "Redis: ${REDIS_URL}"
echo "Weaviate: ${WEAVIATE_URL}"
echo ""

if [[ "$FULL" == "1" ]]; then
  echo "Starting Docker profile full (mongo + redis + weaviate)..."
  local_stack_compose --profile full up -d
else
  echo "Ensuring host Mongo..."
  if ! local_stack_mongo_ok; then
    if command -v brew >/dev/null 2>&1 && brew services list 2>/dev/null | grep -q mongodb; then
      echo "Starting mongodb via brew services..."
      brew services start mongodb-community 2>/dev/null || brew services start mongodb-community@7.0 2>/dev/null || true
      sleep 2
    fi
  fi
  if ! local_stack_mongo_ok; then
    echo "WARN: Mongo not reachable at ${MONGODB_URI}"
    echo "  brew services start mongodb-community"
    echo "  or: bash scripts/local_stack_up.sh --full"
  else
    echo "Mongo OK"
  fi

  echo "Ensuring host Redis..."
  bash scripts/ensure_redis.sh || {
    echo "WARN: Redis not up — bot price cache/bus may degrade"
  }

  if [[ "$WITH_WEAVIATE" == "1" ]]; then
    echo "Starting Weaviate (Docker)..."
    local_stack_compose up -d weaviate
  else
    echo "Skipping Weaviate (--no-weaviate)"
  fi
fi

echo ""
bash scripts/local_stack_health.sh --infra || true
echo ""
echo "Next:"
echo "  bash scripts/local_stack_bot.sh       # xagent-test"
echo "  bash scripts/local_stack_hermes.sh    # xagent-hermes (Tier B)"
echo "  bash scripts/local_stack_sidecars.sh  # santiment + oracle (Tier C)"
echo "  bash scripts/local_stack_health.sh"
