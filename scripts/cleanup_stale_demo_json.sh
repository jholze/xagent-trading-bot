#!/usr/bin/env bash
# Archive stale demo ledger JSON files after Mongo is confirmed as SOT.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/scripts/dev_local_mongo.sh"

export DEMO_MODE=1
export DEMO_LEDGER_BACKEND=mongo

MIN_ORDERS="${MIN_DEMO_ORDERS:-50}"
ORDER_COUNT="$(python3 - <<'PY'
from data_manager import load_orders
print(len(load_orders("demo").get("orders") or []))
PY
)"

if [[ "$ORDER_COUNT" -lt "$MIN_ORDERS" ]]; then
  echo "ABORT: demo Mongo has only $ORDER_COUNT orders (need >= $MIN_ORDERS)."
  echo "Run scripts/sync_demo_ledger_from_railway.sh before archiving JSON."
  exit 1
fi

ARCHIVE="$ROOT/data/archived_demo_json_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$ARCHIVE"
MOVED=0
for f in orders.demo.json positions.demo.json live_trade_history.demo.json trade_history.demo.json; do
  if [[ -f "$ROOT/$f" ]]; then
    mv "$ROOT/$f" "$ARCHIVE/"
    echo "archived $f"
    MOVED=$((MOVED + 1))
  fi
done

echo "Mongo SOT confirmed ($ORDER_COUNT orders). Archived $MOVED file(s) to:"
echo "  $ARCHIVE"