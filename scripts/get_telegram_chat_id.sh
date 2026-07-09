#!/usr/bin/env bash
# Print chat_id after you sent /start (or any message) to your dev bot in Telegram.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
source scripts/source_bot_env.sh

if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
  echo "❌ TELEGRAM_BOT_TOKEN not set — add to .env.local"
  exit 1
fi

echo "Fetching updates for your dev bot..."
echo "(Send /start to the bot in Telegram if the list is empty.)"
echo ""

python3 - <<'PY'
import json
import os
import subprocess
import sys

token = os.environ["TELEGRAM_BOT_TOKEN"]
proc = subprocess.run(
    ["curl", "-sS", f"https://api.telegram.org/bot{token}/getUpdates"],
    capture_output=True,
    text=True,
    timeout=20,
    check=True,
)
data = json.loads(proc.stdout)

if not data.get("ok"):
    print("getUpdates failed:", data, file=sys.stderr)
    sys.exit(1)

updates = data.get("result") or []
if not updates:
    print("No messages yet.")
    print("1. Open your NEW bot in Telegram")
    print("2. Press Start or send /start")
    print("3. Run this script again")
    sys.exit(0)

seen = {}
for u in updates:
    msg = u.get("message") or u.get("edited_message") or {}
    chat = msg.get("chat") or {}
    cid = chat.get("id")
    if cid is None:
        continue
    seen[cid] = {
        "chat_id": cid,
        "type": chat.get("type"),
        "title": chat.get("title") or chat.get("username") or chat.get("first_name"),
        "text": (msg.get("text") or "")[:80],
    }

print("Found chat(s):")
for row in seen.values():
    print(f"  TELEGRAM_CHAT_ID={row['chat_id']}  ({row['type']}: {row['title']})  last: {row['text']!r}")

if len(seen) == 1:
    only = next(iter(seen))
    print("")
    print(f"Add to .env.local:  TELEGRAM_CHAT_ID={only}")
PY