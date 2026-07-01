#!/usr/bin/env python3
"""Register Telegram webhook for Railway (static public URL, no ngrok)."""

from __future__ import annotations

import os
import sys

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def resolve_public_base_url() -> str | None:
    base = (os.getenv("WEBHOOK_BASE_URL") or "").strip().rstrip("/")
    if base:
        return base if base.startswith("http") else f"https://{base}"
    domain = (os.getenv("RAILWAY_PUBLIC_DOMAIN") or "").strip()
    if domain:
        return f"https://{domain}"
    return None


def register_webhook(*, drop_pending: bool = False) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("TELEGRAM_BOT_TOKEN not set — webhook registration skipped", file=sys.stderr)
        return False

    public = resolve_public_base_url()
    if not public:
        print("WEBHOOK_BASE_URL / RAILWAY_PUBLIC_DOMAIN not set — webhook registration skipped", file=sys.stderr)
        return False

    webhook_url = f"{public}/"
    api = f"https://api.telegram.org/bot{token}"

    try:
        probe = requests.post(
            webhook_url,
            json={"update_id": 0},
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        if probe.status_code != 200:
            print(f"Webhook probe failed: HTTP {probe.status_code} for {webhook_url}", file=sys.stderr)
            return False
    except Exception as exc:
        print(f"Webhook probe error: {exc}", file=sys.stderr)
        return False

    resp = requests.post(
        f"{api}/setWebhook",
        data={
            "url": webhook_url,
            "drop_pending_updates": "true" if drop_pending else "false",
            "allowed_updates": '["message","callback_query"]',
        },
        timeout=15,
    )
    data = resp.json() if resp.status_code == 200 else {}
    if data.get("ok"):
        print(f"Telegram webhook registered: {webhook_url}")
        return True
    print(f"setWebhook failed: {resp.text[:300]}", file=sys.stderr)
    return False


if __name__ == "__main__":
    ok = register_webhook(drop_pending=os.getenv("WEBHOOK_DROP_PENDING", "0") == "1")
    sys.exit(0 if ok else 1)