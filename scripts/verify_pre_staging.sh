#!/usr/bin/env bash
# Local gate before staging push: MT routing, ledger scope, trending sync, cycle summary.
# Usage: bash scripts/verify_pre_staging.sh
# Mirrors deploy_staging.sh pre-push smoke (smoke_mt_demo.sh) with a broader pytest slice.
set -euo pipefail
cd "$(dirname "$0")/.."

unset MONGO_URL
export MONGODB_URI="${MONGODB_URI:-mongodb://127.0.0.1:27017}"
export MONGODB_DB="${MONGODB_DB:-xagent_pytest}"
export MONGODB_TEST_DB="${MONGODB_TEST_DB:-xagent_pytest}"

echo "=== Pre-staging verification (local) ==="
echo "Mongo: ${MONGODB_URI} db=${MONGODB_DB}"
echo ""

if ! python3 -c "from storage.mongo_client import ping_database; ping_database(test=True)" 2>/dev/null; then
  echo "ERROR: local Mongo not reachable at ${MONGODB_URI}"
  echo "  brew services start mongodb-community"
  echo "  source scripts/dev_local_mongo.sh"
  exit 1
fi

echo "=== 1/2 smoke_mt_demo (MT + ledger + trending sync) ==="
bash scripts/smoke_mt_demo.sh

echo ""
echo "=== 2/2 extended pytest (demo portfolio / ledger repair guards) ==="
python3 -m pytest \
  tests/unit/test_dry_run_watchlist.py \
  tests/unit/test_daily_portfolio_demo_orders.py \
  tests/unit/test_reconcile_all_tenant_trade_history.py \
  tests/unit/test_dry_run_portfolio.py \
  -q --tb=line

echo ""
echo "OK — safe to push staging (bash scripts/deploy_staging.sh)"