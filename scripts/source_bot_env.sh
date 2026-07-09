#!/usr/bin/env bash
# Load shared secrets (.env) then local overrides (.env.local) for dev Telegram bot.
# Usage: source scripts/source_bot_env.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ ! -f "${ROOT}/.env" ]]; then
  echo "❌ ${ROOT}/.env missing (API keys, etc.)"
  return 1 2>/dev/null || exit 1
fi

# shellcheck disable=SC1091
set -a
source "${ROOT}/.env"
if [[ -f "${ROOT}/.env.local" ]]; then
  # shellcheck disable=SC1091
  source "${ROOT}/.env.local"
  echo "📱 Using .env.local overrides (dev Telegram bot)"
fi
set +a