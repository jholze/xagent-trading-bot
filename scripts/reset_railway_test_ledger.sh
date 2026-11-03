#!/usr/bin/env bash
# Railway test ledger reset, routed through scripts/ledger_epoch.py.
#
# The old path (reset_demo_ledger.py) made no backup, reset only the default
# tenant and left the live-scope peak_equity behind. This wrapper now runs
# backup -> verify --against-mongo -> reset, and is a DRY RUN unless
# RESET_LEDGER_APPLY=1.
#
# Usage:
#   scripts/reset_railway_test_ledger.sh henry default            # backup + verify + dry run
#   RESET_LEDGER_APPLY=1 scripts/reset_railway_test_ledger.sh henry default
#
# Before RESET_LEDGER_APPLY=1: stop xagent-test and every side service that
# writes the ledger (exit-radar, dca-sniper, gainer-signal, mcp, hermes).
# Apply also needs RAILWAY_REDIS_SERVICE (the Redis xagent-test uses) and, if
# the bot sets one, the same REDIS_KEY_PREFIX: ledger_epoch.py takes the bot's
# writer lease per tenant/scope and refuses while any writer still holds it.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ $# -lt 1 ]]; then
  echo "REFUSED: name every tenant explicitly, e.g. $0 henry default" >&2
  exit 2
fi
TENANT_ARGS=()
for t in "$@"; do
  TENANT_ARGS+=(--tenant "$t")
done

if ! command -v railway >/dev/null 2>&1; then
  echo "ERROR: railway CLI missing" >&2
  exit 1
fi

MONGO_SERVICE="${RAILWAY_MONGO_SERVICE:-MongoDB-AeF7}"
RAILWAY_ENV="${RAILWAY_ENVIRONMENT:-test}"
echo "Fetching Railway Mongo public URL (${MONGO_SERVICE}, env=${RAILWAY_ENV})..."
MONGO_PUBLIC="$(
  railway variables --service "$MONGO_SERVICE" --environment "$RAILWAY_ENV" --json 2>/dev/null \
    | python3 -c "import json,sys; print(json.load(sys.stdin).get('MONGO_PUBLIC_URL',''))"
)"
if [[ -z "$MONGO_PUBLIC" ]]; then
  echo "ERROR: MONGO_PUBLIC_URL missing" >&2
  exit 1
fi

REDIS_PUBLIC=""
if [[ "${RESET_LEDGER_APPLY:-}" == "1" ]]; then
  if [[ -z "${RAILWAY_REDIS_SERVICE:-}" ]]; then
    echo "REFUSED: RESET_LEDGER_APPLY=1 needs RAILWAY_REDIS_SERVICE (writer lease)" >&2
    exit 2
  fi
  REDIS_PUBLIC="$(
    railway variables --service "$RAILWAY_REDIS_SERVICE" --environment "$RAILWAY_ENV" --json 2>/dev/null \
      | python3 -c "import json,sys; print(json.load(sys.stdin).get('REDIS_PUBLIC_URL',''))"
  )"
  if [[ -z "$REDIS_PUBLIC" ]]; then
    echo "ERROR: REDIS_PUBLIC_URL missing on $RAILWAY_REDIS_SERVICE" >&2
    exit 1
  fi
fi

OUT_DIR="${RESET_BACKUP_DIR:-auswertungen/epoch_backup_$(date -u +%Y%m%dT%H%M%SZ)}"
run() {
  env -u MONGODB_URI -u PYTEST_RUNNING -u PYTEST_CURRENT_TEST \
    MONGO_URL="$MONGO_PUBLIC" REDIS_URL="$REDIS_PUBLIC" \
    MONGODB_DB="${MONGODB_DB:-xagent_test}" \
    DEMO_MODE=1 DEMO_LEDGER_BACKEND=mongo DEMO_ALLOW_REMOTE_MONGO=1 \
    FORCE_OPERATOR_MONGO=1 ALLOW_DEV_DB_MUTATION=1 \
    python3 scripts/ledger_epoch.py "$@"
}

run backup "${TENANT_ARGS[@]}" --out-dir "$OUT_DIR"
run verify --dir "$OUT_DIR" --against-mongo
if [[ "${RESET_LEDGER_APPLY:-}" == "1" ]]; then
  run reset --dir "$OUT_DIR" "${TENANT_ARGS[@]}" --apply --confirm-writers-stopped
  echo ""
  echo "Restart xagent-test and the side services to reload in-memory positions."
else
  run reset --dir "$OUT_DIR" "${TENANT_ARGS[@]}"
fi
echo "Backup kept in $OUT_DIR (copy it off this machine)."
