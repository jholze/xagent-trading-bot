#!/usr/bin/env bash
# Ensure local Redis is running (required for price cache + bus streams).
set -euo pipefail

REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379/0}"

if command -v redis-cli >/dev/null 2>&1; then
  if redis-cli -u "$REDIS_URL" ping >/dev/null 2>&1; then
    echo "Redis OK ($REDIS_URL)"
    exit 0
  fi
else
  echo "WARN: redis-cli not found — install: brew install redis"
fi

if command -v brew >/dev/null 2>&1; then
  if brew services list 2>/dev/null | grep -q 'redis'; then
    echo "Starting Redis via brew services..."
    brew services start redis >/dev/null 2>&1 || true
    sleep 1
    if redis-cli -u "$REDIS_URL" ping >/dev/null 2>&1; then
      echo "Redis started ($REDIS_URL)"
      exit 0
    fi
  fi
fi

if command -v redis-server >/dev/null 2>&1; then
  echo "Starting redis-server in background..."
  redis-server --daemonize yes --port 6379 >/dev/null 2>&1 || true
  sleep 1
  if redis-cli -u "$REDIS_URL" ping >/dev/null 2>&1; then
    echo "Redis started ($REDIS_URL)"
    exit 0
  fi
fi

echo "FAIL: Redis not reachable at $REDIS_URL"
exit 1