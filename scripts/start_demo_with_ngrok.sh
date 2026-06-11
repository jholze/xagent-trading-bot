#!/usr/bin/env bash
# Start X-Agent bot in demo mode + fresh ngrok tunnel + Telegram webhook
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  echo "❌ .env missing (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID required)"
  exit 1
fi

# shellcheck disable=SC1091
source .env

if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
  echo "❌ TELEGRAM_BOT_TOKEN not set in .env"
  exit 1
fi

BOT_PID=""
NGROK_PID=""

cleanup() {
  echo ""
  echo "Stopping..."
  [[ -n "$BOT_PID" ]] && kill "$BOT_PID" 2>/dev/null || true
  [[ -n "$NGROK_PID" ]] && kill "$NGROK_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "🧹 Cleaning up old bot/ngrok processes..."
bash scripts/stop_bot.sh

wait_for_port_free() {
  local port=$1
  local i
  for i in $(seq 1 20); do
    if ! lsof -ti tcp:"$port" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
  done
  echo "❌ Port $port still in use"
  exit 1
}

wait_for_port_free 5000
wait_for_port_free 4040

echo "🧪 Starting bot (demo mode)..."
DEMO_MODE=1 python3 aria_bot.py --demo &
BOT_PID=$!

echo "⏳ Waiting for bot on :5000..."
for i in $(seq 1 30); do
  if curl -sf http://127.0.0.1:5000/health >/dev/null 2>&1; then
    echo "   Bot ready (pid $BOT_PID)"
    break
  fi
  if [[ $i -eq 30 ]]; then
    echo "❌ Bot did not start on port 5000"
    exit 1
  fi
  sleep 1
done

echo "🌐 Starting fresh ngrok tunnel on port 5000..."
: > ngrok.log
ngrok http 5000 --log=stdout >> ngrok.log 2>&1 &
NGROK_PID=$!

PUBLIC_URL=""
echo "⏳ Waiting for ngrok public URL..."
for i in $(seq 1 30); do
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
    if [[ -n "$PUBLIC_URL" ]]; then
      break
    fi
  fi
  sleep 1
done

if [[ -z "$PUBLIC_URL" ]]; then
  echo "❌ Could not read ngrok public URL (is ngrok authenticated?)"
  echo "   Check: ngrok config check"
  tail -20 ngrok.log || true
  exit 1
fi

WEBHOOK_URL="${PUBLIC_URL}/"
echo "🔗 Ngrok URL: $PUBLIC_URL"

echo "🩺 Verifying tunnel → bot..."
for i in $(seq 1 10); do
  HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    -X POST "$WEBHOOK_URL" \
    -H "Content-Type: application/json" \
    -H "ngrok-skip-browser-warning: true" \
    -d '{}' || echo "000")
  if [[ "$HTTP_CODE" == "200" ]]; then
    echo "   Tunnel OK (HTTP $HTTP_CODE)"
    break
  fi
  if [[ $i -eq 10 ]]; then
    echo "❌ Tunnel verification failed (HTTP $HTTP_CODE)"
    echo "   Telegram webhook would not work. Check ngrok.log"
    tail -20 ngrok.log || true
    exit 1
  fi
  sleep 2
done

echo "🔗 Registering Telegram webhook: $WEBHOOK_URL"
WEBHOOK_RESULT=$(curl -sf "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  -d "url=${WEBHOOK_URL}" \
  -d "drop_pending_updates=true" \
  -d 'allowed_updates=["message","callback_query"]')
echo "$WEBHOOK_RESULT" | python3 -m json.tool

WEBHOOK_OK=$(echo "$WEBHOOK_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('ok', False))")
if [[ "$WEBHOOK_OK" != "True" ]]; then
  echo "❌ setWebhook failed"
  exit 1
fi

sleep 2
WEBHOOK_INFO=$(curl -sf "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo")
LAST_ERR=$(echo "$WEBHOOK_INFO" | python3 -c "import sys,json; print(json.load(sys.stdin)['result'].get('last_error_message') or '')")
REGISTERED_URL=$(echo "$WEBHOOK_INFO" | python3 -c "import sys,json; print(json.load(sys.stdin)['result'].get('url') or '')")

if [[ "$REGISTERED_URL" != "$WEBHOOK_URL" ]]; then
  echo "❌ Webhook URL mismatch: $REGISTERED_URL"
  exit 1
fi

if [[ -n "$LAST_ERR" ]]; then
  echo "⚠️  Telegram reports webhook error: $LAST_ERR"
  echo "   Retrying webhook registration in 5s..."
  sleep 5
  curl -sf "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
    -d "url=${WEBHOOK_URL}" \
    -d "drop_pending_updates=true" \
    -d 'allowed_updates=["message","callback_query"]' >/dev/null
fi

python3 -c "
import os, requests
from dotenv import load_dotenv
load_dotenv()
token = os.getenv('TELEGRAM_BOT_TOKEN')
chat = os.getenv('TELEGRAM_CHAT_ID')
if token and chat:
    url = f'https://api.telegram.org/bot{token}/sendMessage'
    text = (
        '✅ <b>Bot + ngrok neu gestartet</b>\n\n'
        f'<b>Webhook:</b> ${PUBLIC_URL}\n'
        '<b>Modus:</b> Paper (Demo)\n\n'
        'Sende /help zum Testen.'
    )
    requests.post(url, json={'chat_id': int(chat), 'text': text, 'parse_mode': 'HTML'}, timeout=10)
" 2>/dev/null || true

echo ""
echo "✅ Ready!"
echo "   Bot:    http://127.0.0.1:5000 (demo mode, pid $BOT_PID)"
echo "   Ngrok:  $PUBLIC_URL (pid $NGROK_PID)"
echo "   Webhook: $REGISTERED_URL"
echo "   Telegram: send /help to your bot"
echo "   Stop:     bash scripts/stop_bot.sh"
echo "   Press Ctrl+C to stop"
echo ""

wait "$BOT_PID"