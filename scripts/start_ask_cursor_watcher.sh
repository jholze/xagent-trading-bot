#!/usr/bin/env bash
# Cursor-side watcher: notifies the agent when /ask arrives via Telegram.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data logs
exec python3 -u scripts/ask_bridge_watcher.py --interval 0.5 2>&1 | tee -a logs/ask_cursor_watcher.log