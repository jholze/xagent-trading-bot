#!/usr/bin/env bash
# Push local demo Mongo ledger → Railway test stack (MongoDB-AeF7).
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v railway >/dev/null 2>&1; then
  echo "ERROR: railway CLI missing"
  exit 1
fi

# shellcheck disable=SC1091
source scripts/dev_local_mongo.sh
export DEMO_MODE=1
export DEMO_LEDGER_BACKEND=mongo
export MONGODB_DB="${MONGODB_DB:-xagent_test}"

BUNDLE="$(mktemp)"
trap 'rm -f "$BUNDLE"' EXIT

echo "Exporting local demo ledger..."
python3 scripts/demo_ledger_bundle.py export >"$BUNDLE"

ORDER_COUNT="$(python3 -c "import json; print(len(json.load(open('$BUNDLE'))['orders']['orders']))")"
if [[ "$ORDER_COUNT" -lt 1 ]]; then
  echo "ERROR: local export empty"
  exit 1
fi

echo "Fetching Railway test Mongo public URL (MongoDB-AeF7)..."
MONGO_PUBLIC="$(
  railway variables --service MongoDB-AeF7 --environment test --json 2>/dev/null \
    | python3 -c "import json,sys; print(json.load(sys.stdin).get('MONGO_PUBLIC_URL',''))"
)"
if [[ -z "$MONGO_PUBLIC" ]]; then
  echo "ERROR: MONGO_PUBLIC_URL missing for MongoDB-AeF7 (test)"
  exit 1
fi

echo "Importing ${ORDER_COUNT} orders into Railway test Mongo..."
env -u MONGODB_URI \
  MONGO_URL="$MONGO_PUBLIC" \
  MONGODB_DB="$MONGODB_DB" \
  DEMO_MODE=1 \
  DEMO_LEDGER_BACKEND=mongo \
  python3 scripts/demo_ledger_bundle.py import --file "$BUNDLE"

echo "Done. Restart xagent-test to pick up in-memory positions:"
echo "  railway environment link test && railway service link xagent-test && railway redeploy"