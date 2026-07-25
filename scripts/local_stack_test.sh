#!/usr/bin/env bash
# Local counter-test gate: infra + pre-staging smokes + optional broader unit slice.
# Usage:
#   bash scripts/local_stack_test.sh              # infra health + verify_pre_staging
#   bash scripts/local_stack_test.sh --unit        # + fast unit suite (no integration marker)
#   bash scripts/local_stack_test.sh --full-unit   # full tests/unit (slower)
#   bash scripts/local_stack_test.sh --up          # ensure stack --full first
set -euo pipefail
cd "$(dirname "$0")/.."

# shellcheck disable=SC1091
source scripts/local_stack_lib.sh
local_stack_load_env

DO_UP=0
DO_UNIT=0
DO_FULL_UNIT=0
for arg in "$@"; do
  case "$arg" in
    --up) DO_UP=1 ;;
    --unit) DO_UNIT=1 ;;
    --full-unit) DO_FULL_UNIT=1 ;;
    -h|--help)
      echo "Usage: bash scripts/local_stack_test.sh [--up] [--unit|--full-unit]"
      exit 0
      ;;
  esac
done

PY="$(local_stack_python)"
export PATH="$(dirname "$PY"):${PATH}"
echo "=== Local stack test ==="
echo "Python: $PY ($("$PY" -c 'import sys; print(sys.version.split()[0])'))"
echo "Mongo:  ${MONGODB_URI} db=${MONGODB_DB} (pytest db=${MONGODB_TEST_DB:-xagent_pytest})"
echo ""

# Isolate from Railway + pin pytest DB
unset MONGO_URL
export MONGODB_URI="${MONGODB_URI:-mongodb://127.0.0.1:27017}"
export MONGODB_DB="${MONGODB_DB:-xagent_test}"
export MONGODB_TEST_DB="${MONGODB_TEST_DB:-xagent_pytest}"
export REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379/0}"

if [[ "$DO_UP" == "1" ]]; then
  bash scripts/local_stack_up.sh --full
fi

echo "--- 1/3 Infra health ---"
bash scripts/local_stack_health.sh --infra

echo ""
echo "--- 2/3 Pre-staging verification ---"
# verify_pre_staging uses python3 from PATH — ensure our PY wins via symlink-less override
if [[ "$(command -v python3)" != "$PY" ]]; then
  # Prefer explicit interpreter for pytest inside verify when possible
  export LOCAL_STACK_PYTHON="$PY"
fi
bash scripts/verify_pre_staging.sh

if [[ "$DO_UNIT" == "1" || "$DO_FULL_UNIT" == "1" ]]; then
  echo ""
  echo "--- 3/3 Unit tests ---"
  unset MONGO_URL
  export MONGODB_URI="${MONGODB_URI}"
  export MONGODB_TEST_DB="${MONGODB_TEST_DB}"
  export MONGODB_DB="${MONGODB_TEST_DB}"
  if [[ "$DO_FULL_UNIT" == "1" ]]; then
    "$PY" -m pytest tests/unit -q --tb=line -m "not integration"
  else
    # Fast, high-signal slice for day-to-day feature counter-tests
    # (omit network-flaky memory news polls from the default gate)
    "$PY" -m pytest \
      tests/unit/test_dry_run_watchlist.py \
      tests/unit/test_dry_run_portfolio.py \
      tests/unit/test_daily_portfolio_demo_orders.py \
      tests/unit/test_reconcile_all_tenant_trade_history.py \
      tests/unit/test_coin_eligibility.py \
      tests/unit/test_order_service.py \
      tests/unit/test_order_ledger_v2.py \
      tests/unit/test_sensor_entry_memory.py \
      tests/unit/test_order_isolation.py \
      -q --tb=line
  fi
else
  echo ""
  echo "--- 3/3 Unit tests skipped (pass --unit or --full-unit) ---"
fi

echo ""
echo "OK — local counter-test gate green"
echo "Next: exercise feature with bot (bash scripts/local_stack_bot.sh) then deploy_staging from staging branch"
