#!/usr/bin/env bash
# Install macOS LaunchAgent for daily report (23:55) — more reliable than cron on macOS.
set -euo pipefail

BOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST_SRC="$BOT_DIR/scripts/com.xagent.daily-auswertung.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.xagent.daily-auswertung.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$BOT_DIR/logs"
cp "$PLIST_SRC" "$PLIST_DST"
launchctl bootout "gui/$(id -u)/com.xagent.daily-auswertung" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
launchctl enable "gui/$(id -u)/com.xagent.daily-auswertung"
echo "Installed LaunchAgent: $PLIST_DST (daily 23:55)"
echo "Test now: bash $BOT_DIR/scripts/cron_daily_auswertung.sh"