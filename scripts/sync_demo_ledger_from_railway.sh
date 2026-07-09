#!/usr/bin/env bash
# Copy Railway demo Mongo ledger → local Mongo so /positions matches live.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v railway >/dev/null 2>&1; then
  echo "WARN: railway CLI missing — skip demo ledger sync"
  exit 0
fi

# shellcheck disable=SC1091
source scripts/dev_local_mongo.sh
export DEMO_MODE=1
export DEMO_LEDGER_BACKEND=mongo

BUNDLE="$(mktemp)"
trap 'rm -f "$BUNDLE"' EXIT

# BOT_STACK=staging|test → staging Mongo (MongoDB-AeF7); default production (MongoDB-mPbb)
MONGO_SERVICE="${RAILWAY_MONGO_SERVICE:-}"
if [[ -z "$MONGO_SERVICE" ]]; then
  case "${BOT_STACK:-production}" in
    staging|test) MONGO_SERVICE="MongoDB-AeF7" ;;
    *) MONGO_SERVICE="MongoDB-mPbb" ;;
  esac
fi
RAILWAY_ENV="${RAILWAY_ENVIRONMENT:-}"
if [[ -z "$RAILWAY_ENV" ]]; then
  case "${BOT_STACK:-production}" in
    staging|test) RAILWAY_ENV="test" ;;
    *) RAILWAY_ENV="production" ;;
  esac
fi

echo "Fetching Railway Mongo public URL (${MONGO_SERVICE}, env=${RAILWAY_ENV})..."
MONGO_PUBLIC="$(
  railway variables --service "$MONGO_SERVICE" --environment "$RAILWAY_ENV" --json 2>/dev/null \
    | python3 -c "import json,sys; print(json.load(sys.stdin).get('MONGO_PUBLIC_URL',''))"
)"
if [[ -z "$MONGO_PUBLIC" ]]; then
  echo "WARN: MONGO_PUBLIC_URL missing — local ledger unchanged"
  exit 0
fi

echo "Exporting live demo ledger from Railway → bundle..."
# IMPORTANT: unset local MONGODB_URI so resolve_mongo_uri() uses MONGO_URL (public proxy).
# railway run + dev_local_mongo would otherwise export localhost by mistake.
if ! env -u MONGODB_URI -u PYTEST_RUNNING -u PYTEST_CURRENT_TEST \
  MONGO_URL="$MONGO_PUBLIC" \
  MONGODB_DB="${MONGODB_DB:-xagent_test}" \
  DEMO_MODE=1 \
  DEMO_LEDGER_BACKEND=mongo \
  DEMO_ALLOW_REMOTE_MONGO=1 \
  FORCE_OPERATOR_MONGO=1 \
  python3 scripts/demo_ledger_bundle.py export >"$BUNDLE" 2>/dev/null; then
  echo "WARN: Railway export failed — local ledger unchanged"
  exit 0
fi

if ! python3 -c "import json; d=json.load(open('$BUNDLE')); assert len(d.get('orders',{}).get('orders',[]))>0" 2>/dev/null; then
  echo "WARN: Invalid/empty export bundle — local ledger unchanged"
  exit 0
fi

ORDER_COUNT="$(python3 -c "import json; print(len(json.load(open('$BUNDLE'))['orders']['orders']))")"
echo "Importing ${ORDER_COUNT} orders into local Mongo (${MONGODB_DB})..."
python3 scripts/demo_ledger_bundle.py import --file "$BUNDLE"
echo "Demo ledger sync OK (${ORDER_COUNT} orders)"