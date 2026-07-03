#!/usr/bin/env bash
# Read-only portfolio impact check against Railway Mongo (before deploy).
# Usage: bash scripts/railway_portfolio_impact.sh
# Requires: railway CLI linked to project (railway link)
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v railway >/dev/null 2>&1; then
  echo "❌ railway CLI not found (brew install railway)"
  exit 1
fi

echo "=== Railway Portfolio Impact (read-only via railway run) ==="
railway status 2>/dev/null | head -8 || true
echo ""

run_railway() {
  railway run -- "$@"
}

run_railway python3 - <<'PY'
import sys
sys.path.insert(0, ".")
from storage.mongo_client import ping_database, resolve_database_name, mongo_uri_host, resolve_mongo_uri

if not ping_database():
    raise SystemExit("Mongo ping failed")
uri = resolve_mongo_uri()
host = mongo_uri_host(uri)
if host in ("127.0.0.1", "localhost", "::1"):
    raise SystemExit(
        "Still on localhost — run 'railway link' in repo root first."
    )
print(f"Connected: {host} / {resolve_database_name()}")
PY

echo ""
echo "--- Open Positions: Rule Signals (live prices) ---"
run_railway python3 scripts/open_positions_exit_preview.py

echo ""
echo "--- 30d Backtest vs Actual ---"
run_railway python3 scripts/backtest_exit_rules_30d.py