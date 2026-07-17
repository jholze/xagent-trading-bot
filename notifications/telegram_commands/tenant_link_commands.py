"""Link tenants via Telegram /start deep links (invite flow)."""

from __future__ import annotations

import re

from core.operator_notify import notify_tenant_linked
from notifications.telegram_commands.onboarding_commands import _is_valid_tenant_id, _normalize_tenant_id
from storage.tenant_registry import link_tenant_owner_chat
from telegram_notifier import build_tenant_invite_link, send_telegram_message

_START_RE = re.compile(r"^/start(?:@\w+)?(?:\s+(.+))?$", re.IGNORECASE)


def parse_start_payload(text: str) -> str:
    m = _START_RE.match((text or "").strip())
    if not m:
        return ""
    raw = (m.group(1) or "").strip()
    if not raw:
        return ""
    return raw.split()[0].strip()


def try_link_tenant_from_start(text: str, chat_id: str | int) -> tuple[bool, str]:
    """If text is /start <tenant_id>, link chat to tenant. Returns (handled, user_message)."""
    payload = parse_start_payload(text)
    if not payload:
        return False, ""
    tid = _normalize_tenant_id(payload)
    if not _is_valid_tenant_id(tid):
        return True, "❌ Ungültiger Einladungs-Code."
    ok, msg = link_tenant_owner_chat(tid, chat_id)
    if ok:
        notify_tenant_linked(tid, chat_id)
        try:
            from notifications.telegram_commands.menu_commands import open_menu_for_chat
            from notifications.telegram_commands.menu_i18n import resolve_ui_language

            lang = resolve_ui_language(None, tid)
            open_menu_for_chat(chat_id, lang=lang)
        except Exception as e:
            try:
                from logger import log

                log(f"open_menu_for_chat after link failed (tenant={tid}): {e}", "WARNING")
            except Exception:
                pass
    return True, msg


def handle(text: str) -> bool:
    from notifications.telegram_commands.command_context import current_chat_id

    payload = parse_start_payload(text)
    if not text.strip().lower().startswith("/start"):
        return False
    if payload:
        handled, msg = try_link_tenant_from_start(text, current_chat_id())
        if msg:
            send_telegram_message(msg)
        return handled

    # Already linked (operator or satellite): open role-appropriate navigation.
    chat_id = current_chat_id()
    try:
        from core.tenant_routing import resolve_incoming_tenant
        from notifications.telegram_commands.menu_commands import open_menu_for_chat
        from notifications.telegram_commands.menu_i18n import resolve_ui_language

        route = resolve_incoming_tenant(chat_id=chat_id)
        if chat_id and not route.rejected:
            lang = resolve_ui_language(None, route.tenant_id)
            open_menu_for_chat(chat_id, lang=lang)
            return True
    except Exception as e:
        try:
            from logger import log

            log(f"/start open menu failed (chat={chat_id}): {e}", "WARNING")
        except Exception:
            pass

    send_telegram_message(
        "👋 Willkommen beim xAgent Trading Bot.\n\n"
        "Hast du einen Einladungs-Link vom Operator? Einfach antippen.\n"
        "Sonst: <code>/myid</code> für deine Chat-ID."
    )
    return True


def invite_message_for_operator(tenant_id: str) -> str:
    link = build_tenant_invite_link(tenant_id)
    return (
        f"📨 <b>Einladung für <code>{tenant_id}</code></b>\n\n"
        f"Schick Henry diese Nachricht (privat reicht):\n\n"
        f"<code>Öffne den Bot und tippe Start:\n{link}</code>\n\n"
        f"Danach ist sein Chat automatisch mit <code>{tenant_id}</code> verbunden."
    )