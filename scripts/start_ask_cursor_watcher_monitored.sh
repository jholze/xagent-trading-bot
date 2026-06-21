#!/usr/bin/env bash
# Start ask watcher for terminal→agent coupling.
# In Grok/Cursor: run as background task with output pattern @@CURSOR_ASK_ACTION@@
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data logs
echo "Watching for @@CURSOR_ASK_ACTION@@ and @@CURSOR_ASK_NOTIFY@@ (cursor_only)"
exec python3 -u scripts/ask_bridge_watcher.py --interval 0.5 2>&1 | tee -a logs/ask_cursor_watcher.log