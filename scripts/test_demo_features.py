#!/usr/bin/env python3
"""Test all Telegram features via webhook (local + ngrok)."""
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv

load_dotenv()

CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
COMMANDS = [
    "/help",
    "/mode",
    "/mode paper",
    "/gate",
    "/risk",
    "/list",
    "/positions",
    "/xsignals",
    "/listx",
    "/xposts",
    "/xaccuracy",
    "/cmc",
    "/sandbox",
    "/tracktest",
    "/add DOGE",
    "/list",
    "/buy DOGE 20",
    "/sell",
    "/live_cancel",
]


def post_webhook(base_url: str, command: str) -> tuple[int, str]:
    payload = {
        "update_id": int(time.time() * 1000),
        "message": {
            "message_id": int(time.time()),
            "text": command,
            "chat": {"id": CHAT_ID, "type": "private"},
            "from": {"id": CHAT_ID, "is_bot": False, "first_name": "Test"},
        },
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        base_url.rstrip("/") + "/",
        data=data,
        headers={
            "Content-Type": "application/json",
            "ngrok-skip-browser-warning": "true",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode()[:80]
    except Exception as e:
        return 0, str(e)[:120]


def get_ngrok_url() -> str:
    with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=5) as resp:
        tunnels = json.loads(resp.read()).get("tunnels", [])
    for t in tunnels:
        url = t.get("public_url", "")
        if url.startswith("https://"):
            return url
    return ""


def main():
    ngrok = get_ngrok_url()
    targets = [("local", "http://127.0.0.1:5000")]
    if ngrok:
        targets.append(("ngrok", ngrok))

    print("=" * 60)
    print("DEMO FEATURE WEBHOOK TEST")
    print("=" * 60)

    ok = fail = 0
    for label, base in targets:
        print(f"\n▶ Target: {label} ({base})")
        for cmd in COMMANDS:
            status, body = post_webhook(base, cmd)
            if status == 200:
                ok += 1
                print(f"  ✅ {cmd}")
            else:
                fail += 1
                print(f"  ❌ {cmd} — HTTP {status} {body}")
            time.sleep(0.4)

    print("\n" + "=" * 60)
    print(f"Results: {ok} OK, {fail} failed across {len(targets)} endpoint(s)")
    print("Check Telegram for 🧪 [DEMO] replies.")
    print("=" * 60)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())