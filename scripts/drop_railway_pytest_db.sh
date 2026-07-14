#!/usr/bin/env bash
# Drop xagent_pytest on Railway staging Mongo (never touches xagent_test).
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v railway >/dev/null 2>&1; then
  echo "ERROR: railway CLI missing"
  exit 1
fi

MONGO_SERVICE="${RAILWAY_MONGO_SERVICE:-MongoDB-AeF7}"
RAILWAY_ENV="${RAILWAY_ENVIRONMENT:-test}"
DRY_RUN="${DROP_PYTEST_DRY_RUN:-}"

echo "Fetching Railway Mongo public URL (${MONGO_SERVICE}, env=${RAILWAY_ENV})..."
MONGO_PUBLIC="$(
  railway variables --service "$MONGO_SERVICE" --environment "$RAILWAY_ENV" --json 2>/dev/null \
    | python3 -c "import json,sys; print(json.load(sys.stdin).get('MONGO_PUBLIC_URL',''))"
)"
if [[ -z "$MONGO_PUBLIC" ]]; then
  echo "ERROR: MONGO_PUBLIC_URL missing on ${MONGO_SERVICE}"
  exit 1
fi

ARGS=()
if [[ "$DRY_RUN" == "1" ]]; then
  ARGS+=(--dry-run)
else
  ARGS+=(--yes)
fi

echo "Dropping xagent_pytest on Railway (xagent_test stays untouched)..."
env -u MONGODB_URI -u PYTEST_RUNNING -u PYTEST_CURRENT_TEST \
  MONGO_URL="$MONGO_PUBLIC" \
  MONGODB_DB=xagent_test \
  MONGODB_TEST_DB=xagent_pytest \
  DEMO_ALLOW_REMOTE_MONGO=1 \
  ALLOW_REMOTE_MONGO_DROP=1 \
  python3 scripts/drop_railway_pytest_db.py "${ARGS[@]}"