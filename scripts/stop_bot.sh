#!/usr/bin/env bash
# Stop bot, ngrok, and free ports 5000 / 4040
set -euo pipefail

echo "🛑 Stopping trading bot stack..."

pkill -f "aria_bot.py" 2>/dev/null || true
pkill -f "ngrok http" 2>/dev/null || true
pkill -f "start_demo_with_ngrok.sh" 2>/dev/null || true

for port in 5000 4040; do
  pids=$(lsof -ti tcp:"$port" 2>/dev/null || true)
  if [[ -n "$pids" ]]; then
    echo "   Freeing port $port (pid $pids)"
    kill $pids 2>/dev/null || true
  fi
done

sleep 2

for port in 5000 4040; do
  if lsof -ti tcp:"$port" >/dev/null 2>&1; then
    echo "   Force-kill port $port"
    lsof -ti tcp:"$port" | xargs kill -9 2>/dev/null || true
  fi
done

echo "✅ Stopped"