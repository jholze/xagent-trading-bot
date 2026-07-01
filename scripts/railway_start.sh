#!/usr/bin/env bash
# Railway production start — demo mode + Mongo ledger (matches local demo, no ngrok).
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== X-Agent Railway start ==="

# Demo mode (same as local scripts/start_demo_with_ngrok.sh)
export DEMO_MODE=1
export MONGODB_DB="${MONGODB_DB:-xagent_test}"
# Full Mongo ledger on Railway (ephemeral disk — no JSON SOT for orders/positions)
export DEMO_LEDGER_BACKEND="${DEMO_LEDGER_BACKEND:-mongo}"

# Reduce CPU on small Railway plans (override in Railway vars if desired)
export RAILWAY_DEPLOY=1

if [[ ! -f watchlist.demo.json && -f watchlist.json ]]; then
  echo "Seeding watchlist.demo.json from watchlist.json"
  cp watchlist.json watchlist.demo.json
fi

echo "Mongo DB: ${MONGODB_DB} | Demo ledger backend: ${DEMO_LEDGER_BACKEND}"

python3 - <<'PY' || { echo "MongoDB ping failed — check MONGODB_URI"; exit 1; }
import os, sys
sys.path.insert(0, ".")
from storage.mongo_client import ping_database, resolve_database_name, resolve_mongo_uri
db = resolve_database_name()
uri = resolve_mongo_uri()
print(f"Mongo URI host: {uri.split('@')[-1] if '@' in uri else uri}")
print(f"Mongo DB: {db}")
if not ping_database():
    raise SystemExit("ping failed")
print("MongoDB OK")
PY

echo "Seeding demo Mongo ledger if needed..."
python3 scripts/railway_seed_demo_mongo.py || echo "WARN: demo Mongo seed skipped"

# Register Telegram webhook (Railway public domain — replaces ngrok)
if [[ -n "${WEBHOOK_BASE_URL:-}" || -n "${RAILWAY_PUBLIC_DOMAIN:-}" ]]; then
  echo "Registering Telegram webhook..."
  python3 scripts/register_railway_webhook.py || echo "WARN: webhook registration failed (will retry via watchdog)"
else
  echo "WARN: No WEBHOOK_BASE_URL / RAILWAY_PUBLIC_DOMAIN — set in Railway service settings"
fi

echo "Registering Telegram command menu..."
python3 - <<'PY' 2>/dev/null || echo "WARN: command menu registration skipped"
import sys
sys.path.insert(0, ".")
from notifications.telegram_commands.command_menu import register_bot_commands
register_bot_commands()
PY

PORT="${PORT:-5000}"
echo "Starting aria_bot.py --demo on 0.0.0.0:${PORT}"
exec python3 aria_bot.py --demo