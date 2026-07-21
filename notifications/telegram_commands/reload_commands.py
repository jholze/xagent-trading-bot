"""Telegram /reload — soft hot-reload (A1–A6)."""

from __future__ import annotations

from notifications.telegram_commands.command_context import current_chat_id
from services.reload_registry import (
    SCOPES,
    format_reload_help_html,
    format_reload_report_html,
    normalize_scopes,
    run_reload,
)
from telegram_notifier import send_telegram_message


def handle(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    lower = raw.lower()
    if lower not in ("/reload", "/hotreload") and not lower.startswith("/reload "):
        return False

    parts = raw.split(maxsplit=1)
    arg = parts[1].strip().lower() if len(parts) > 1 else ""

    if not arg or arg in ("help", "?", "h"):
        send_telegram_message(format_reload_help_html())
        return True

    # Validate scopes early for a friendly error
    if arg != "all":
        tokens = [p for p in arg.replace(",", " ").split() if p]
        unknown = [t for t in tokens if t not in SCOPES and t != "all"]
        if unknown or not tokens:
            send_telegram_message(
                "❌ Unbekannter Reload-Scope.\n\n" + format_reload_help_html()
            )
            return True

    scopes = normalize_scopes(arg)
    actor = str(current_chat_id() or "")
    report = run_reload(scopes, source="telegram", actor=actor)
    send_telegram_message(format_reload_report_html(report))
    return True
