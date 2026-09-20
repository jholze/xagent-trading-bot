"""Telegram /reload — soft hot-reload (A1–A6)."""

from __future__ import annotations

from notifications.telegram_commands.command_context import (
    cancel_keyboard,
    clear_context,
    current_chat_id,
    get_context,
    set_chat_id,
    set_context,
)
from notifications.telegram_i18n import t
from services.reload_registry import (
    SCOPES,
    format_reload_help_html,
    format_reload_report_html,
    normalize_scopes,
    run_reload,
)
from telegram_notifier import answer_callback_query, send_telegram_message

RELOAD_CALLBACK_PREFIX = "reload_ok:"


def _execute_reload(scopes_arg: str) -> None:
    scopes = normalize_scopes(scopes_arg)
    actor = str(current_chat_id() or "")
    report = run_reload(scopes, source="telegram", actor=actor)
    send_telegram_message(format_reload_report_html(report))


def _prompt_reload_confirm(scopes_arg: str) -> None:
    cid = current_chat_id()
    if cid:
        set_context(cid, "reload", state="reload_awaiting_confirm", scopes=scopes_arg)
    send_telegram_message(
        t("reload_confirm", scopes=scopes_arg),
        reply_markup={
            "inline_keyboard": [
                [
                    {
                        "text": t("reload_confirm_btn_ok"),
                        "callback_data": RELOAD_CALLBACK_PREFIX,
                    }
                ],
                cancel_keyboard()[0],
            ]
        },
    )


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

    _prompt_reload_confirm(arg)
    return True


def _callback_chat_id(callback_query: dict, log_label: str):
    from logger import log

    callback_id = (callback_query or {}).get("id")
    if callback_id:
        answer_callback_query(callback_id)
    chat_id = ((callback_query or {}).get("message") or {}).get("chat") or {}
    chat_id = chat_id.get("id") if isinstance(chat_id, dict) else None
    if not chat_id:
        log(f"{log_label} callback missing chat id", "WARNING")
        return None
    set_chat_id(chat_id)
    return chat_id


def handle_callback(callback_query: dict) -> bool:
    data = str((callback_query or {}).get("data") or "")
    if not data.startswith(RELOAD_CALLBACK_PREFIX):
        return False
    chat_id = _callback_chat_id(callback_query, "reload_ok")
    if not chat_id:
        return True
    entry = get_context(chat_id)
    meta = (entry or {}).get("meta") or {}
    if (
        not entry
        or entry.get("command") != "reload"
        or str(meta.get("state") or "") != "reload_awaiting_confirm"
    ):
        send_telegram_message(t("reload_confirm_expired"))
        return True
    scopes_arg = str(meta.get("scopes") or "").strip()
    clear_context(chat_id)
    if not scopes_arg:
        send_telegram_message(t("reload_confirm_expired"))
        return True
    _execute_reload(scopes_arg)
    return True
