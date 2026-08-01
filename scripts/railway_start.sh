#!/usr/bin/env bash
# Railway production start — demo mode + Mongo ledger (matches local demo, no ngrok).
set -euo pipefail
cd "$(dirname "$0")/.."

# Same monorepo image can run auxiliary services when selected by Railway service name.
if [[ "${RAILWAY_SERVICE_NAME:-}" == "xagent-santiment" || "${RUN_SANTIMENT_SIDECAR:-}" == "1" ]]; then
  echo "=== Santiment sidecar start ==="
  export PYTHONUNBUFFERED=1
  exec python3 -m services.santiment_sidecar
fi
if [[ "${RAILWAY_SERVICE_NAME:-}" == "xagent-market-oracle" || "${RUN_MARKET_ORACLE:-}" == "1" ]]; then
  echo "=== Market oracle start ==="
  export PYTHONUNBUFFERED=1
  exec python3 -m services.market_oracle
fi
if [[ "${RAILWAY_SERVICE_NAME:-}" == "xagent-hermes" || "${RUN_HERMES:-}" == "1" ]]; then
  echo "=== Hermes + Trading Memory service start ==="
  export PYTHONUNBUFFERED=1
  export DEMO_MODE="${DEMO_MODE:-1}"
  export MONGODB_DB="${MONGODB_DB:-xagent_test}"
  # Hermes is read-only on ledger; never run ledger repair/seed
  exec python3 -m intelligence.memory.service
fi
if [[ "${RAILWAY_SERVICE_NAME:-}" == "xagent-exit-radar" || "${RAILWAY_SERVICE_NAME:-}" == "exit-radar" || "${RUN_EXIT_RADAR:-}" == "1" ]]; then
  echo "=== Exit radar + Gate WS sidecar start ==="
  export PYTHONUNBUFFERED=1
  export RUN_EXIT_RADAR=1
  export DEMO_MODE="${DEMO_MODE:-1}"
  export DEMO_LEDGER_BACKEND="${DEMO_LEDGER_BACKEND:-mongo}"
  export MONGODB_DB="${MONGODB_DB:-xagent_test}"
  export DEMO_ALLOW_REMOTE_MONGO="${DEMO_ALLOW_REMOTE_MONGO:-1}"
  export EXIT_REALTIME_OWNER="${EXIT_REALTIME_OWNER:-sidecar}"
  exec python3 -m services.exit_radar_sidecar
fi

echo "=== X-Agent Railway start ==="
python3 scripts/write_build_meta.py 2>/dev/null || true
python3 - <<'PY' 2>/dev/null || true
import sys
sys.path.insert(0, ".")
from core.build_info import get_build_info
b = get_build_info()
print(f"Build revision: {b['commit']} @ {b['branch']}" + (" (dirty)" if b['dirty'] else ""))
PY

# Persistent volume for logs + WQE scores (mount /app/logs on xagent-test)
# Railway injects RAILWAY_VOLUME_MOUNT_PATH when a volume is attached.
if [[ -n "${RAILWAY_VOLUME_MOUNT_PATH:-}" ]]; then
  echo "Volume mount: ${RAILWAY_VOLUME_MOUNT_PATH} (name=${RAILWAY_VOLUME_NAME:-?})"
  mkdir -p "${RAILWAY_VOLUME_MOUNT_PATH}"
  # Prefer volume for LOG_DIR if mount is /app/logs (default WORKDIR layout)
  if [[ "${RAILWAY_VOLUME_MOUNT_PATH}" == "/app/logs" ]] || [[ "${RAILWAY_VOLUME_MOUNT_PATH}" == *"/logs" ]]; then
    export WQE_DATA_DIR="${WQE_DATA_DIR:-${RAILWAY_VOLUME_MOUNT_PATH}}"
    echo "WQE_DATA_DIR=${WQE_DATA_DIR} (scores + wqe_events survive redeploy)"
  fi
fi
mkdir -p logs

# Demo mode (same as local scripts/start_demo_with_ngrok.sh)
export DEMO_MODE=1
export MONGODB_DB="${MONGODB_DB:-xagent_test}"
# Full Mongo ledger on Railway (orders/positions in Mongo; logs on volume)
export DEMO_LEDGER_BACKEND="${DEMO_LEDGER_BACKEND:-mongo}"

# Reduce CPU on small Railway plans (override in Railway vars if desired)
export RAILWAY_DEPLOY=1
export BOT_TIMEZONE="${BOT_TIMEZONE:-Europe/Berlin}"
export TZ="${TZ:-$BOT_TIMEZONE}"

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

echo "Reconciling demo ledger (positions + cash)..."
python3 - <<'PY' || echo "WARN: demo ledger reconcile skipped"
import os, sys
sys.path.insert(0, ".")
os.environ.setdefault("DEMO_MODE", "1")
os.environ.setdefault("DEMO_LEDGER_BACKEND", "mongo")
from data_manager import reconcile_demo_trade_history_on_startup, resolve_ledger_scope
from services.ledger_sync import rebuild_positions_from_orders, sync_positions_on_startup

scope = resolve_ledger_scope()
open_count = rebuild_positions_from_orders(scope)
sync_positions_on_startup()
reconcile_demo_trade_history_on_startup()
print(f"Demo ledger reconcile OK ({open_count} open positions)")
PY

# Register Telegram webhook (Railway public domain — replaces ngrok)
if [[ -n "${WEBHOOK_BASE_URL:-}" || -n "${RAILWAY_PUBLIC_DOMAIN:-}" ]]; then
  echo "Registering Telegram webhook..."
  python3 scripts/register_railway_webhook.py || echo "WARN: webhook registration failed (will retry via watchdog)"
else
  echo "WARN: No WEBHOOK_BASE_URL / RAILWAY_PUBLIC_DOMAIN — set in Railway service settings"
fi

PORT="${PORT:-5000}"
echo "Starting aria_bot.py --demo on 0.0.0.0:${PORT}"
exec python3 aria_bot.py --demo