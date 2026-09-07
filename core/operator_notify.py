"""Notify the bot operator (TELEGRAM_CHAT_ID) outside tenant context."""

from __future__ import annotations

import os

from logger import log


def resolve_operator_chat_id() -> str:
    """Operator chat: env TELEGRAM_CHAT_ID, else default tenant registry."""
    op = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    if op:
        return op
    try:
        from core.tenant_context import DEFAULT_TENANT
        from storage.tenant_registry import get_tenant

        doc = get_tenant(DEFAULT_TENANT) or {}
        return str((doc.get("telegram") or {}).get("owner_chat_id") or "").strip()
    except Exception:
        return ""


def notify_operator(text: str, *, parse_mode: str = "HTML") -> bool:
    """Send to operator — never via tenant owner routing.

    Goes through ``send_telegram_message`` (urgent lane) when the publisher is
    running; falls back to ``_send_telegram_direct`` otherwise.
    """
    from bus.schemas import PRIORITY_URGENT
    from telegram_notifier import send_telegram_message

    op_chat = resolve_operator_chat_id()
    if not op_chat:
        log("Operator notify skipped: TELEGRAM_CHAT_ID not configured", "WARNING")
        return False
    ok = send_telegram_message(
        text, chat_id=op_chat, parse_mode=parse_mode, priority=PRIORITY_URGENT
    )
    if not ok:
        log(f"Operator notify failed for chat {op_chat}", "WARNING")
    return ok


def notify_tenant_linked(tenant_id: str, user_chat_id: str | int) -> bool:
    cid = str(user_chat_id or "").strip()
    tid = (tenant_id or "").strip().lower()
    if not tid or not cid:
        return False
    op_chat = resolve_operator_chat_id()
    if op_chat and cid == op_chat:
        return False
    return notify_operator(
        f"🔗 <b>{tid}</b> ist jetzt verbunden.\n"
        f"Chat-ID: <code>{cid}</code>\n\n"
        f"Henry kann <code>/menu</code> und <code>/help</code> nutzen."
    )