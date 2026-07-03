#!/usr/bin/env bash
# Point production Telegram bot webhook back to Railway (after local ngrok hijacked it).
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  echo "❌ .env missing"
  exit 1
fi

# Prod token only — never load .env.local
# shellcheck disable=SC1091
set -a
source .env
set +a

WEBHOOK_BASE_URL="${WEBHOOK_BASE_URL:-https://xagent-bot-production.up.railway.app}"
export WEBHOOK_BASE_URL
python3 scripts/register_railway_webhook.py