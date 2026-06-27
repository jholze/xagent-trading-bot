#!/usr/bin/env bash
# Install LaunchAgent: restart demo bot (Andro) + ngrok twice daily (08:00, 20:00 local).
set -euo pipefail

BOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST_SRC="$BOT_DIR/scripts/com.xagent.demo-restart.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.xagent.demo-restart.plist"

chmod +x "$BOT_DIR/scripts/restart_demo_scheduled.sh"
mkdir -p "$HOME/Library/LaunchAgents" "$BOT_DIR/logs" "$BOT_DIR/run"
cp "$PLIST_SRC" "$PLIST_DST"

launchctl bootout "gui/$(id -u)/com.xagent.demo-restart" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
launchctl enable "gui/$(id -u)/com.xagent.demo-restart"

echo "Installed: $PLIST_DST"
echo "Schedule: 08:00 and 20:00 (local time)"
echo "Logs: $BOT_DIR/logs/demo_restart_scheduled.log"
echo "Test now: bash $BOT_DIR/scripts/restart_demo_scheduled.sh"