#!/usr/bin/env bash
# Reset Railway test demo ledger to clean starting balance (default \$100k).
set -euo pipefail
cd "$(dirname "$0")/.."

BALANCE="${1:-100000}"
YES="${RESET_LEDGER_YES:-}"

if ! command -v railway >/dev/null 2>&1; then
  echo "ERROR: railway CLI missing"
  exit 1
fi

MONGO_SERVICE="${RAILWAY_MONGO_SERVICE:-MongoDB-AeF7}"
RAILWAY_ENV="${RAILWAY_ENVIRONMENT:-test}"

echo "Fetching Railway Mongo public URL (${MONGO_SERVICE}, env=${RAILWAY_ENV})..."
MONGO_PUBLIC="$(
  railway variables --service "$MONGO_SERVICE" --environment "$RAILWAY_ENV" --json 2>/dev/null \
    | python3 -c "import json,sys; print(json.load(sys.stdin).get('MONGO_PUBLIC_URL',''))"
)"
if [[ -z "$MONGO_PUBLIC" ]]; then
  echo "ERROR: MONGO_PUBLIC_URL missing"
  exit 1
fi

ARGS=(--balance "$BALANCE")
if [[ "$YES" == "1" ]]; then
  ARGS+=(--yes)
fi

echo "Resetting Railway test ledger to \$${BALANCE}..."
env -u MONGODB_URI \
  MONGO_URL="$MONGO_PUBLIC" \
  MONGODB_DB="${MONGODB_DB:-xagent_test}" \
  DEMO_MODE=1 \
  DEMO_LEDGER_BACKEND=mongo \
  DEMO_ALLOW_REMOTE_MONGO=1 \
  ALLOW_DEV_DB_MUTATION=1 \
  python3 scripts/reset_demo_ledger.py "${ARGS[@]}"

echo ""
echo "Restart xagent-test to reload in-memory positions:"
echo "  railway environment link test && railway service link xagent-test && railway redeploy -y"