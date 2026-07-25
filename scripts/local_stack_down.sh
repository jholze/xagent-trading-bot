#!/usr/bin/env bash
# Stop local stack sidecars / hermes / cortex + docker infra.
# Does not stop brew mongo/redis. Bot: use --bot or scripts/stop_bot.sh
# Usage:
#   bash scripts/local_stack_down.sh
#   bash scripts/local_stack_down.sh --bot
#   bash scripts/local_stack_down.sh --volumes   # docker compose down -v
set -euo pipefail
cd "$(dirname "$0")/.."

# shellcheck disable=SC1091
source scripts/local_stack_lib.sh
local_stack_load_env

STOP_BOT=0
DROP_VOLUMES=0
for arg in "$@"; do
  case "$arg" in
    --bot) STOP_BOT=1 ;;
    --volumes) DROP_VOLUMES=1 ;;
    -h|--help)
      echo "Usage: bash scripts/local_stack_down.sh [--bot] [--volumes]"
      exit 0
      ;;
  esac
done

echo "=== Local stack down ==="
for name in hermes santiment market-oracle cortex; do
  local_stack_stop_pidfile "$name"
done

if [[ "$STOP_BOT" == "1" ]]; then
  echo "Stopping bot/ngrok..."
  bash scripts/stop_bot.sh || true
fi

if [[ -f "$(local_stack_compose_file)" ]]; then
  if [[ "$DROP_VOLUMES" == "1" ]]; then
    echo "Docker compose down -v (profile full)..."
    local_stack_compose --profile full down -v || local_stack_compose down -v || true
  else
    echo "Docker compose down..."
    local_stack_compose --profile full down || local_stack_compose down || true
  fi
fi

echo "Done. Host Mongo/Redis left running (brew)."
