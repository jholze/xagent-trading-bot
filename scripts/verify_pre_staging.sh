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

# Prefer Python with project deps (pymongo/pytest). Override: LOCAL_STACK_PYTHON=/path/to/python
if [[ -z "${LOCAL_STACK_PYTHON:-}" ]]; then
  # shellcheck disable=SC1091
  source "$(dirname "$0")/local_stack_lib.sh" 2>/dev/null || true
  if declare -F local_stack_python >/dev/null 2>&1; then
    LOCAL_STACK_PYTHON="$(local_stack_python)"
  else
    LOCAL_STACK_PYTHON="$(command -v python3)"
  fi
fi
PY="${LOCAL_STACK_PYTHON}"
export LOCAL_STACK_PYTHON="$PY"

echo "=== Pre-staging verification (local) ==="
echo "Mongo: ${MONGODB_URI} db=${MONGODB_DB}"
echo "Python: ${PY}"
echo ""

if ! "$PY" -c "from storage.mongo_client import ping_database; raise SystemExit(0 if ping_database(test=True) else 1)" 2>/dev/null; then
  # Docker mongo may be up while pymongo path fails — try mongosh / container
  mongo_ok=0
  if command -v mongosh >/dev/null 2>&1; then
    mongosh --quiet "${MONGODB_URI}" --eval 'db.adminCommand({ping:1}).ok' 2>/dev/null | grep -q 1 && mongo_ok=1
  fi
  if [[ "$mongo_ok" != "1" ]] && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx 'xagent-local-mongo'; then
    docker exec xagent-local-mongo mongosh --quiet --eval 'db.adminCommand({ping:1}).ok' 2>/dev/null | grep -q 1 && mongo_ok=1
  fi
  if [[ "$mongo_ok" != "1" ]]; then
    echo "ERROR: local Mongo not reachable at ${MONGODB_URI}"
    echo "  bash scripts/local_stack_up.sh --full"
    echo "  or: brew services start mongodb-community"
    exit 1
  fi
  if ! "$PY" -c "import pymongo" 2>/dev/null; then
    echo "ERROR: Python lacks pymongo: ${PY}"
    echo "  Install deps: ${PY} -m pip install -r requirements.txt"
    echo "  Or set LOCAL_STACK_PYTHON to a 3.13 env with deps"
    exit 1
  fi
fi

echo "=== 1/2 smoke_mt_demo (MT + ledger + trending sync) ==="
bash scripts/smoke_mt_demo.sh

echo ""
echo "=== 2/2 extended pytest (demo portfolio / ledger repair guards) ==="
"$PY" -m pytest \
  tests/unit/test_dry_run_watchlist.py \
  tests/unit/test_daily_portfolio_demo_orders.py \
  tests/unit/test_reconcile_all_tenant_trade_history.py \
  tests/unit/test_dry_run_portfolio.py \
  -q --tb=line

echo ""
echo "OK — safe to push staging (bash scripts/deploy_staging.sh)"