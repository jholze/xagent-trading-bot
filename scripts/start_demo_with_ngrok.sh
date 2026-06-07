#!/usr/bin/env bash
# Start X-Agent bot in demo mode + ngrok + Telegram webhook
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

echo "🧪 Starting bot (demo mode)..."
DEMO_MODE=1 python3 aria_bot.py --demo &
BOT_PID=$!
sleep 4

if ! curl -sf http://127.0.0.1:5000/ -X POST -H "Content-Type: application/json" -d '{}' >/dev/null 2>&1; then
  echo "⚠️  Bot may still be starting..."
fi

echo "🌐 Starting ngrok on port 5000..."
ngrok http 5000 --log=stdout > ngrok.log 2>&1 &
NGROK_PID=$!
sleep 3

PUBLIC_URL=$(curl -s http://127.0.0.1:4040/api/tunnels | python3 -c "
import sys, json
tunnels = json.load(sys.stdin).get('tunnels', [])
https = [t['public_url'] for t in tunnels if t.get('public_url','').startswith('https')]
print(https[0] if https else '')
")

if [[ -z "$PUBLIC_URL" ]]; then
  echo "❌ Could not read ngrok public URL (is ngrok authenticated?)"
  exit 1
fi

WEBHOOK_URL="${PUBLIC_URL}/"
echo "🔗 Registering Telegram webhook: $WEBHOOK_URL"
curl -sf "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  -d "url=${WEBHOOK_URL}" \
  -d "drop_pending_updates=true" \
  -d 'allowed_updates=["message"]' | python3 -m json.tool

echo ""
echo "✅ Ready!"
echo "   Bot:    http://127.0.0.1:5000 (demo mode)"
echo "   Ngrok:  $PUBLIC_URL"
echo "   Telegram: send /help to your bot"
echo "   Press Ctrl+C to stop"
echo ""

wait "$BOT_PID"