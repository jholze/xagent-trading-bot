#!/usr/bin/env bash
# Emergency restore: local default:demo ledger → Railway operator Mongo.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v railway >/dev/null 2>&1; then
  echo "ERROR: railway CLI required"
  exit 1
fi

# shellcheck disable=SC1091
source scripts/dev_local_mongo.sh
export DEMO_MODE=1
export DEMO_LEDGER_BACKEND=mongo

BUNDLE="$(mktemp)"
trap 'rm -f "$BUNDLE"' EXIT

echo "Exporting local operator demo ledger..."
python3 scripts/demo_ledger_bundle.py export >"$BUNDLE"
ORDER_COUNT="$(python3 -c "import json; print(len(json.load(open('$BUNDLE'))['orders']['orders']))")"
echo "Local bundle: ${ORDER_COUNT} orders"

if [[ "$ORDER_COUNT" -lt 50 ]]; then
  echo "ERROR: local bundle too small — aborting restore"
  exit 1
fi

MONGO_SERVICE="${RAILWAY_MONGO_SERVICE:-MongoDB-AeF7}"
RAILWAY_ENV="${RAILWAY_ENVIRONMENT:-test}"
MONGO_PUBLIC="$(
  railway variables --service "$MONGO_SERVICE" --environment "$RAILWAY_ENV" --json 2>/dev/null \
    | python3 -c "import json,sys; print(json.load(sys.stdin).get('MONGO_PUBLIC_URL',''))"
)"
if [[ -z "$MONGO_PUBLIC" ]]; then
  echo "ERROR: MONGO_PUBLIC_URL missing"
  exit 1
fi

echo "Importing into Railway (${ORDER_COUNT} orders)..."
env -u MONGODB_URI -u PYTEST_RUNNING -u PYTEST_CURRENT_TEST \
  MONGO_URL="$MONGO_PUBLIC" \
  MONGODB_DB="${MONGODB_DB:-xagent_test}" \
  DEMO_MODE=1 \
  DEMO_LEDGER_BACKEND=mongo \
  DEMO_ALLOW_REMOTE_MONGO=1 \
  FORCE_OPERATOR_MONGO=1 \
  python3 scripts/demo_ledger_bundle.py import --file "$BUNDLE"

echo "Restore import complete."