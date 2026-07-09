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
            "TELEGRAM_CHAT_ID (.env or .env.local; bash scripts/get_telegram_chat_id.sh)",
            file=sys.stderr,
        )
        return 1

    import requests

    custom = os.getenv("NOTIFY_TEXT", "").strip()
    if "--text" in sys.argv:
        idx = sys.argv.index("--text")
        if idx + 1 < len(sys.argv):
            custom = sys.argv[idx + 1].strip()

    if custom:
        text = custom
    else:
        public_url = (os.getenv("PUBLIC_URL") or (sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "")).strip()
        if not public_url:
            print("⚠️  Startup Telegram skipped — no PUBLIC_URL", file=sys.stderr)
            return 1

        from core.runtime_identity import format_build_line, format_startup_message

        os.environ.setdefault("BOT_STACK", "local")
        text = format_startup_message().replace(
            "Details: <code>/stand</code>",
            f"<b>Webhook:</b> {public_url}\n\nDetails: <code>/mode</code>",
        )
        if format_build_line() not in text:
            text += f"\n\n{format_build_line()}"
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