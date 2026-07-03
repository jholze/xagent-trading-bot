#!/usr/bin/env python3
"""Send Telegram startup message after local demo restart."""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pathlib import Path

from dotenv import load_dotenv


def main() -> int:
    load_dotenv(Path(ROOT) / ".env")
    load_dotenv(Path(ROOT) / ".env.local", override=True)

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        print(
            "⚠️  Startup Telegram skipped — set TELEGRAM_BOT_TOKEN and "
            "TELEGRAM_CHAT_ID in .env.local (bash scripts/get_telegram_chat_id.sh)",
            file=sys.stderr,
        )
        return 1

    public_url = (os.getenv("PUBLIC_URL") or (sys.argv[1] if len(sys.argv) > 1 else "")).strip()
    if not public_url:
        print("⚠️  Startup Telegram skipped — no PUBLIC_URL", file=sys.stderr)
        return 1

    from core.build_info import format_build_line

    import requests

    text = (
        "✅ <b>Bot + ngrok neu gestartet</b>\n\n"
        f"<b>Webhook:</b> {public_url}\n"
        "<b>Modus:</b> Paper (Demo)\n"
        "<b>Mongo:</b> xagent_test\n"
        f"{format_build_line()}\n\n"
        "Sende /help zum Testen."
    )
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": int(chat), "text": text, "parse_mode": "HTML"},
        timeout=15,
    )
    data = resp.json()
    if not data.get("ok"):
        print(f"⚠️  Startup Telegram failed: {data.get('description', data)}", file=sys.stderr)
        return 1
    print("📲 Startup Telegram message sent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())