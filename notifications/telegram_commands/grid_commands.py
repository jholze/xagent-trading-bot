"""Telegram /grid — which active coins run grid mode."""

from __future__ import annotations

import threading

from core.tenant_context import tenant_context, tenant_snapshot
from notifications.telegram_commands.command_context import current_chat_id
from notifications.telegram_commands.menu_i18n import current_language
from services.grid_status_service import build_grid_status_report, format_grid_status_telegram
from telegram_notifier import send_telegram_message


def _normalize_symbol_arg(arg: str) -> str | None:
    raw = (arg or "").strip().upper()
    if not raw:
        return None
    if raw.endswith("/USDT"):
        return raw
    return f"{raw}/USDT"


def _build_and_send(*, symbol_filter: str | None, chat_id, tenant_id: str, scope: str, owner_chat_id: str, lang: str) -> None:
    try:
        with tenant_context(tenant_id, scope=scope, owner_chat_id=owner_chat_id):
            report = build_grid_status_report(symbol_filter=symbol_filter)
            msg = format_grid_status_telegram(report, lang=lang)
            if len(msg) > 3900:
                msg = msg[:3900] + "\n… <i>(gekürzt)</i>"
            send_telegram_message(msg, chat_id=chat_id or None)
    except Exception as e:
        send_telegram_message(
            f"❌ Grid-Status konnte nicht geladen werden: {e}",
            chat_id=chat_id or None,
        )


def handle(text: str) -> bool:
    parts = (text or "").strip().split()
    if not parts or parts[0] != "/grid":
        return False

    symbol_filter = _normalize_symbol_arg(parts[1]) if len(parts) > 1 else None
    chat_id = current_chat_id()
    tenant_id, scope, owner_chat_id = tenant_snapshot()
    lang = current_language()

    if symbol_filter:
        send_telegram_message(
            f"⏳ <b>Grid</b> — <code>{symbol_filter}</code> wird geladen…",
            chat_id=chat_id or None,
        )
    else:
        send_telegram_message("⏳ <b>Grid-Modus</b> wird geladen…", chat_id=chat_id or None)

    threading.Thread(
        target=_build_and_send,
        kwargs={
            "symbol_filter": symbol_filter,
            "chat_id": chat_id,
            "tenant_id": tenant_id,
            "scope": scope,
            "owner_chat_id": owner_chat_id,
            "lang": lang,
        },
        daemon=True,
        name="grid-cmd",
    ).start()
    return True