#!/usr/bin/env bash
# Export Railway test demo ledger → timestamped JSON backup under auswertungen/.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v railway >/dev/null 2>&1; then
  echo "ERROR: railway CLI missing"
  exit 1
fi

STAMP="$(date +%Y%m%d_%H%M%S)"
OUT="auswertungen/ledger_backup_railway_test_${STAMP}.json"
mkdir -p auswertungen

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

echo "Exporting demo ledger to ${OUT}..."
env -u MONGODB_URI \
  MONGO_URL="$MONGO_PUBLIC" \
  MONGODB_DB="${MONGODB_DB:-xagent_test}" \
  DEMO_MODE=1 \
  DEMO_LEDGER_BACKEND=mongo \
  python3 scripts/demo_ledger_bundle.py export >"$OUT"

ORDER_COUNT="$(python3 -c "import json; print(len(json.load(open('$OUT'))['orders']['orders']))")"
CASH="$(python3 -c "import json; print(json.load(open('$OUT'))['trade_history'].get('virtual_balance',0))")"
echo "Backup OK: ${ORDER_COUNT} orders, cash=\$${CASH} → ${OUT}"