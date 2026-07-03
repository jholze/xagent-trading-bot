#!/usr/bin/env bash
# Read-only portfolio impact check against Railway Mongo (before deploy).
# Usage: bash scripts/railway_portfolio_impact.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  echo "❌ .env missing"
  exit 1
fi

# shellcheck disable=SC1091
set -a
source .env
set +a

if [[ -z "${MONGO_URL:-}" && -z "${MONGODB_URI:-}" ]]; then
  echo "❌ MONGO_URL or MONGODB_URI not set in .env (Railway connection)"
  exit 1
fi

# Railway ledger — never drop, read-only backtest
export DEMO_MODE=1
export MONGODB_DB="${MONGODB_DB:-xagent_test}"
export DEMO_LEDGER_BACKEND="${DEMO_LEDGER_BACKEND:-mongo}"
unset ALLOW_REMOTE_MONGO_DROP

URI="${MONGO_URL:-${MONGODB_URI}}"
HOST=$(python3 -c "from urllib.parse import urlparse; print(urlparse('${URI}').hostname or 'unknown')")

echo "=== Railway Portfolio Impact (read-only) ==="
echo "Mongo host: ${HOST}"
echo "DB: ${MONGODB_DB}"
echo ""

python3 - <<'PY'
import os, sys
sys.path.insert(0, ".")
from storage.mongo_client import ping_database, resolve_database_name, mongo_uri_host, resolve_mongo_uri

if not ping_database():
    raise SystemExit("Mongo ping failed")
uri = resolve_mongo_uri()
host = mongo_uri_host(uri)
if host in ("127.0.0.1", "localhost", "::1"):
    raise SystemExit(
        "Refusing Railway impact check on localhost. "
        "Set MONGO_URL to Railway URI in .env (or export before running)."
    )
print(f"Connected: {host} / {resolve_database_name()}")
PY

echo ""
echo "--- 30d Backtest vs Actual ---"
python3 scripts/backtest_exit_rules_30d.py

echo ""
echo "--- Open Positions: Rule Signals (live prices) ---"
python3 scripts/open_positions_exit_preview.py