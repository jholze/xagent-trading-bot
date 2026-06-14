#!/usr/bin/env python3
"""X / Twitter pipeline health check — API, accounts, posts, sandbox.

Usage:
  python3 scripts/x_health_check.py
  python3 scripts/x_health_check.py --no-telegram
  bash scripts/x_health_check.sh
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=True)

import requests

from data_manager import get_config, load_x_accounts, load_x_posts
from x_data_provider import XApiV2Provider, get_x_provider


def _status(ok: bool) -> str:
    return "✅" if ok else "❌"


def _check_api_token() -> tuple[bool, str]:
    token = (os.getenv("X_API_BEARER_TOKEN") or "").strip()
    if not token:
        return False, "X_API_BEARER_TOKEN fehlt in .env"
    base = os.getenv("X_API_BASE_URL", "https://api.x.com/2").rstrip("/")
    try:
        resp = requests.get(
            f"{base}/users/by/username/CryptoCapo_",
            headers={"Authorization": f"Bearer {token}"},
            params={"user.fields": "id,username"},
            timeout=15,
        )
    except Exception as e:
        return False, f"API nicht erreichbar: {e}"
    if resp.status_code == 200:
        return True, "API OK (User-Lookup CryptoCapo_)"
    if resp.status_code == 402:
        return False, "API-Credits aufgebraucht (402)"
    if resp.status_code == 401:
        return False, "Bearer Token ungültig (401)"
    detail = resp.json().get("detail", resp.text[:120]) if resp.content else resp.status_code
    return False, f"API Fehler {resp.status_code}: {detail}"


def _x_posts_stats() -> dict:
    posts = load_x_posts().get("posts", [])
    real = [p for p in posts if str(p.get("post_id", "")).isdigit()]
    latest = None
    if real:
        latest = real[-1]
    return {
        "total": len(posts),
        "real_ids": len(real),
        "latest_account": (latest or {}).get("account"),
        "latest_action": (latest or {}).get("parsed_action") or (latest or {}).get("action"),
        "latest_coin": (latest or {}).get("coin"),
    }


def _sandbox_count() -> int:
    path = os.path.join(os.path.dirname(__file__), "..", "paper_strategies.json")
    try:
        data = json.load(open(path, encoding="utf-8"))
    except Exception:
        return 0
    return len([h for h in data.get("hypotheses", []) if h.get("status") == "testing"])


def run_checks() -> tuple[list[str], bool]:
    cfg = get_config()
    lines: list[str] = ["<b>🐦 X Health Check</b>", ""]
    all_ok = True

    mock = bool(cfg.get("use_mock_x_data", True))
    grok = bool(cfg.get("use_grok_x_search", True))
    provider = type(get_x_provider(cfg)).__name__
    lines.append(f"{_status(not mock)} Mock-Daten: {'an' if mock else 'aus'}")
    lines.append(f"Provider: <code>{provider}</code> (grok_search={'an' if grok else 'aus'})")
    if mock:
        all_ok = False

    accounts = load_x_accounts()
    enabled = [a for a in accounts if a.get("enabled", True)]
    lines.append(f"{_status(len(enabled) > 0)} Accounts: {len(enabled)} aktiv")
    if not enabled:
        all_ok = False

    if provider == "XApiV2Provider":
        ok, msg = _check_api_token()
        lines.append(f"{_status(ok)} {msg}")
        if not ok:
            all_ok = False
        try:
            new_posts = XApiV2Provider().fetch_new_posts(enabled, limit_per_account=5)
            lines.append(f"{_status(True)} Neue Tweets (API): {len(new_posts)} (0 = bereits importiert)")
        except Exception as e:
            lines.append(f"{_status(False)} Tweet-Fetch: {e}")
            all_ok = False
    elif provider == "GrokXSearchProvider":
        xai = bool(os.getenv("XAI_API_KEY"))
        lines.append(f"{_status(xai)} XAI_API_KEY für Grok X-Search")
        if not xai:
            all_ok = False
    else:
        lines.append(f"⚠️ Mock-Provider — kein Live-X")

    stats = _x_posts_stats()
    has_real = stats["real_ids"] > 0
    lines.append(
        f"{_status(has_real)} x_posts.json: {stats['total']} Einträge, "
        f"{stats['real_ids']} echte Tweet-IDs"
    )
    if stats["latest_account"]:
        lines.append(
            f"Letzter: @{stats['latest_account']} → "
            f"{stats['latest_action']} {stats['latest_coin'] or '—'}"
        )
    if not has_real and provider != "MockXProvider":
        all_ok = False

    sandbox_n = _sandbox_count()
    lines.append(f"📦 Sandbox: {sandbox_n} Hypothesen (testing)")
    lines.append("")
    lines.append(
        "<b>Telegram testen:</b> /listx · /xposts · /xsignals · /tracktest · /testaccount CryptoCapo_ 30"
    )
    lines.append("")
    lines.append(f"<b>Gesamt:</b> {_status(all_ok)} {'OK' if all_ok else 'Probleme — Details oben'}")

    return lines, all_ok


def main() -> int:
    parser = argparse.ArgumentParser(description="X / Twitter pipeline health check")
    parser.add_argument("--no-telegram", action="store_true", help="Nur Terminal-Ausgabe")
    args = parser.parse_args()

    lines, ok = run_checks()
    text = "\n".join(lines)
    plain = text.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", "")
    print(plain)

    if not args.no_telegram:
        if os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"):
            from telegram_notifier import send_telegram_message

            send_telegram_message(text)
            print("\n→ Zusammenfassung an Telegram gesendet.")
        else:
            print("\n→ Telegram übersprungen (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID fehlt).")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())