#!/usr/bin/env bash
# Start local bot = Railway xagent-test (demo + ngrok + Telegram webhook).
# Usage:
#   bash scripts/local_stack_bot.sh
#   bash scripts/local_stack_bot.sh --no-ngrok   # bot only on :5000 (no Telegram webhook)
set -euo pipefail
cd "$(dirname "$0")/.."

# shellcheck disable=SC1091
source scripts/local_stack_lib.sh
local_stack_load_env

NO_NGROK=0
for arg in "$@"; do
  case "$arg" in
    --no-ngrok) NO_NGROK=1 ;;
    -h|--help)
      echo "Usage: bash scripts/local_stack_bot.sh [--no-ngrok]"
      exit 0
      ;;
  esac
done

export BOT_STACK="${BOT_STACK:-local}"
export DEMO_MODE=1
export DEMO_LEDGER_BACKEND=mongo
export REDIS_URL="${REDIS_URL}"
export WEAVIATE_URL="${WEAVIATE_URL}"
# Bot should not bind Hermes port
export PORT="${BOT_PORT:-5000}"

echo "=== Local stack bot (xagent-test) ==="
echo "BOT_STACK=${BOT_STACK} DEMO_MODE=${DEMO_MODE} PORT=${PORT}"
echo "Mongo ${MONGODB_URI} / ${MONGODB_DB} (MONGO_URL unset)"

if [[ "$NO_NGROK" == "1" ]]; then
  # shellcheck disable=SC1091
  source scripts/source_bot_env.sh 2>/dev/null || true
  bash scripts/ensure_redis.sh || true
  # shellcheck disable=SC1091
  source scripts/dev_local_mongo.sh
  echo "Starting aria_bot --demo (no ngrok)..."
  exec env DEMO_MODE=1 DEMO_LEDGER_BACKEND=mongo python3 aria_bot.py --demo
fi

exec bash scripts/start_demo_with_ngrok.sh
