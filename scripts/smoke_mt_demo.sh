#!/usr/bin/env bash
# Fast local check: multi-tenant routing + demo ledger scope (~10s).
# Usage: bash scripts/smoke_mt_demo.sh
set -euo pipefail
cd "$(dirname "$0")/.."

unset MONGO_URL
export MONGODB_URI="${MONGODB_URI:-mongodb://127.0.0.1:27017}"

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

if ! "$PY" -c "from storage.mongo_client import ping_database; raise SystemExit(0 if ping_database(test=True) else 1)" 2>/dev/null; then
  echo "ERROR: local Mongo not reachable at ${MONGODB_URI} (python=${PY})"
  echo "  bash scripts/local_stack_up.sh --full"
  echo "  or: brew services start mongodb-community"
  exit 1
fi

"$PY" scripts/smoke_mt_demo_local.py