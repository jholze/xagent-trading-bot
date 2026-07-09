#!/usr/bin/env bash
# Scheduled demo stack restart: bot + ngrok + Telegram webhook + health checks.
# Safe for launchd/cron (detached, no blocking wait on bot).
set -euo pipefail

BOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$BOT_DIR"
mkdir -p logs run

LOG_FILE="$BOT_DIR/logs/demo_restart_scheduled.log"
exec >>"$LOG_FILE" 2>&1

echo ""
echo "======== $(date '+%Y-%m-%d %H:%M:%S %Z') scheduled demo restart ========"

if [[ ! -f .env ]]; then
  echo "FAIL: .env missing"
  exit 1
fi

if [[ ! -f .env.local ]]; then
  echo "FAIL: .env.local missing — local restart must use DEV bot token (not Railway prod)"
  exit 1
fi

# shellcheck disable=SC1091
source "$BOT_DIR/scripts/source_bot_env.sh"

if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
  echo "FAIL: TELEGRAM_BOT_TOKEN not set"
  exit 1
fi

PROD_BOT_TOKEN="$(grep -E '^TELEGRAM_BOT_TOKEN=' "$BOT_DIR/.env" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")"
if [[ -n "$PROD_BOT_TOKEN" && "$TELEGRAM_BOT_TOKEN" == "$PROD_BOT_TOKEN" ]]; then
  fail "Refusing local restart with production TELEGRAM_BOT_TOKEN — check .env.local"
fi
echo "Telegram webhook will use dev bot token (${TELEGRAM_BOT_TOKEN%%:*}:…)"

notify_telegram() {
  local text="$1"
  if ! python3 "$BOT_DIR/scripts/notify_local_restart.py" --text "$text"; then
    echo "WARN: Telegram notify failed (see stderr above)"
  fi
}

fail() {
  echo "FAIL: $1"
  notify_telegram "❌ <b>Andro Neustart fehlgeschlagen</b>
$1
🕒 $(date '+%H:%M:%S')"
  exit 1
}

pgrep -f "ngrok http" | xargs kill 2>/dev/null || true
bash "$BOT_DIR/scripts/stop_bot.sh"

for port in 5000 4040; do
  for _ in $(seq 1 20); do
    lsof -ti tcp:"$port" >/dev/null 2>&1 || break
    sleep 0.5
  done
  lsof -ti tcp:"$port" >/dev/null 2>&1 && fail "Port $port still in use"
done

bash "$BOT_DIR/scripts/ensure_redis.sh" || fail "Redis not running — price cache requires Redis"

# shellcheck disable=SC1091
source "$BOT_DIR/scripts/dev_local_mongo.sh"
export DEMO_LEDGER_BACKEND=mongo
bash "$BOT_DIR/scripts/sync_demo_ledger_from_railway.sh" || echo "WARN: demo ledger sync skipped"

nohup env DEMO_MODE=1 DEMO_LEDGER_BACKEND=mongo python3 "$BOT_DIR/aria_bot.py" --demo \
  >>"$BOT_DIR/logs/bot_restart.log" 2>&1 &
BOT_PID=$!
echo "$BOT_PID" >"$BOT_DIR/run/aria_bot.pid"
echo "Bot starting pid=$BOT_PID"

BOT_OK=0
for _ in $(seq 1 45); do
  if curl -sf http://127.0.0.1:5000/health >/dev/null 2>&1; then
    BOT_OK=1
    break
  fi
  if ! kill -0 "$BOT_PID" 2>/dev/null; then
    fail "Bot process exited before health check"
  fi
  sleep 1
done
[[ "$BOT_OK" -eq 1 ]] || fail "Bot health on :5000 timeout"

HEALTH=$(curl -sf http://127.0.0.1:5000/health || true)
echo "Health: ${HEALTH:-empty}"
[[ "$HEALTH" == "OK" ]] || fail "Unexpected /health response: $HEALTH"

: >"$BOT_DIR/ngrok.log"
nohup ngrok http 5000 --log=stdout >>"$BOT_DIR/ngrok.log" 2>&1 &
NGROK_PID=$!
echo "$NGROK_PID" >"$BOT_DIR/run/ngrok.pid"
echo "Ngrok starting pid=$NGROK_PID"

PUBLIC_URL=""
for _ in $(seq 1 30); do
  if curl -sf http://127.0.0.1:4040/api/tunnels >/dev/null 2>&1; then
    PUBLIC_URL=$(curl -s http://127.0.0.1:4040/api/tunnels | python3 -c "
import sys, json
try:
    tunnels = json.load(sys.stdin).get('tunnels', [])
    https = [t['public_url'] for t in tunnels if t.get('public_url', '').startswith('https')]
    print(https[0] if https else '')
except Exception:
    print('')
")
    [[ -n "$PUBLIC_URL" ]] && break
  fi
  sleep 1
done
[[ -n "$PUBLIC_URL" ]] || fail "Could not read ngrok public URL"

WEBHOOK_URL="${PUBLIC_URL}/"
echo "Ngrok: $PUBLIC_URL"

TUNNEL_OK=0
for _ in $(seq 1 10); do
  HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    -X POST "$WEBHOOK_URL" \
    -H "Content-Type: application/json" \
    -H "ngrok-skip-browser-warning: true" \
    -d '{}' || echo "000")
  if [[ "$HTTP_CODE" == "200" ]]; then
    TUNNEL_OK=1
    break
  fi
  sleep 2
done
[[ "$TUNNEL_OK" -eq 1 ]] || fail "Tunnel verification failed (HTTP $HTTP_CODE)"

WEBHOOK_RESULT=$(curl -sf "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  -d "url=${WEBHOOK_URL}" \
  -d "drop_pending_updates=false" \
  -d 'allowed_updates=["message","callback_query"]')
WEBHOOK_OK=$(echo "$WEBHOOK_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('ok', False))")
[[ "$WEBHOOK_OK" == "True" ]] || fail "setWebhook failed: $WEBHOOK_RESULT"

REGISTERED=$(curl -sf "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo" | python3 -c "
import sys,json
r=json.load(sys.stdin)['result']
print(r.get('url',''))
err=r.get('last_error_message') or ''
if err:
    print('ERR:'+err, file=sys.stderr)
")
[[ "$REGISTERED" == "$WEBHOOK_URL" ]] || fail "Webhook URL mismatch: $REGISTERED"

python3 -c "
from pathlib import Path
from dotenv import load_dotenv
root = Path('$BOT_DIR')
load_dotenv(root / '.env')
load_dotenv(root / '.env.local', override=True)
from notifications.telegram_commands.command_menu import register_bot_commands
register_bot_commands()
" 2>/dev/null || echo "WARN: command menu registration skipped"

echo "OK bot=$BOT_PID ngrok=$NGROK_PID webhook=$WEBHOOK_URL"
PUBLIC_URL="$PUBLIC_URL" python3 "$BOT_DIR/scripts/notify_local_restart.py" "$PUBLIC_URL" \
  || echo "WARN: startup Telegram notify failed"
exit 0