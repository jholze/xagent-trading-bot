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

echo "Syncing demo ledger Railway → local Mongo (${MONGODB_DB})..."
if ! railway run -- python3 scripts/demo_ledger_bundle.py export >"$BUNDLE" 2>/dev/null; then
  echo "WARN: Railway export failed — local ledger unchanged"
  exit 0
fi

if ! python3 -c "import json; json.load(open('$BUNDLE'))" 2>/dev/null; then
  echo "WARN: Invalid export bundle — local ledger unchanged"
  exit 0
fi

python3 scripts/demo_ledger_bundle.py import --file "$BUNDLE"
echo "Demo ledger sync OK"