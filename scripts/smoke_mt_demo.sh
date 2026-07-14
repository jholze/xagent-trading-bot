#!/usr/bin/env bash
# Fast local check: multi-tenant routing + demo ledger scope (~10s).
# Usage: bash scripts/smoke_mt_demo.sh
set -euo pipefail
cd "$(dirname "$0")/.."

unset MONGO_URL
export MONGODB_URI="${MONGODB_URI:-mongodb://127.0.0.1:27017}"

if ! python3 -c "from storage.mongo_client import ping_database; ping_database(test=True)" 2>/dev/null; then
  echo "ERROR: local Mongo not reachable at ${MONGODB_URI}"
  echo "  brew services start mongodb-community  # or your local Mongo"
  echo "  source scripts/dev_local_mongo.sh"
  exit 1
fi

python3 scripts/smoke_mt_demo_local.py