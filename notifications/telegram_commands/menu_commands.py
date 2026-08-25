"""Categorized Telegram navigation: sections in native menu + reply keyboard."""

from __future__ import annotations

import os

from notifications.telegram_commands.menu_i18n import (
    back_label,
    build_section_help_message,
    callback_unknown_command,
    command_description,
    current_language,
    help_label,
    home_inline,
    home_intro,
    is_back_label,
    is_help_label,
    section_pick,
    section_title,
    title_to_section_id,
)
from notifications.telegram_commands.command_context import (
    clear_active_section,
    clear_context,
    current_chat_id,
    set_active_section,
)
from telegram_notifier import send_telegram_message
from telegram_notifier import (
    answer_callback_query,
    edit_telegram_message,
    send_reply_keyboard,
    send_telegram_buttons,
)

_COMMAND_DISPATCH: dict[str, str] = {
    "positions_full": "/positions full",
}

# Full operator navigation (onboard, live, sandbox/backtest, …).
MENU_SECTIONS_OPERATOR: list[tuple[str, list[str]]] = [
    ("watchlist", ["list", "add", "remove"]),
    ("handel", [
        "positions", "positions_full", "buy", "sell", "short", "cover", "lock", "unlock",
        "orders", "orders_blocked", "orders_month", "plan", "risk",
    ]),
    ("modus", ["mode", "gate", "dryrun", "maxpositions", "live_confirm", "live_cancel", "reload"]),
    ("transparenz", ["morning", "stack", "decisions", "why", "grid", "ask", "hermes", "hermes_last", "cmc", "lc"]),
    ("x", ["addx", "removex", "listx", "xposts", "xsignals", "xaccuracy", "tracktest", "testaccount"]),
    ("tests", ["sandbox", "sandbox_results", "sandbox_promote", "backtest", "backtest_lock", "backtest_results", "hermes_run"]),
    ("hilfe", ["menu", "help", "onboard"]),
]

# Co-tester / satellite tenants (e.g. Henry): no ops, onboard, or live-confirm.
MENU_SECTIONS_SATELLITE: list[tuple[str, list[str]]] = [
    ("watchlist", ["list", "add", "remove"]),
    ("handel", [
        "positions", "positions_full", "buy", "sell", "short", "cover", "lock", "unlock",
        "orders", "orders_blocked", "orders_month", "plan", "risk",
    ]),
    ("modus", ["mode", "gate", "dryrun", "maxpositions", "reload"]),
    ("transparenz", ["morning", "stack", "decisions", "why", "grid", "ask", "hermes", "hermes_last", "cmc", "lc"]),
    ("x", ["addx", "removex", "listx", "xposts", "xsignals", "xaccuracy"]),
    ("hilfe", ["menu", "help"]),
]

# Backward-compatible alias = operator menu (global slash list + tests).
MENU_SECTIONS: list[tuple[str, list[str]]] = MENU_SECTIONS_OPERATOR

_ALL_COMMAND_KEYS = [k for _, keys in MENU_SECTIONS_OPERATOR for k in keys]
_SATELLITE_COMMAND_KEYS = frozenset(k for _, keys in MENU_SECTIONS_SATELLITE for k in keys)


def _operator_chat_id() -> str:
    return (os.getenv("TELEGRAM_CHAT_ID") or "").strip()


def _is_operator_chat(cid: str) -> bool:
    op = _operator_chat_id()
    return bool(cid and op and cid == op)


def menu_role_for(*, chat_id: str | int | None = None, tenant_id: str | None = None) -> str:
    """``operator`` = full menu; ``satellite`` = co-tester (e.g. Henry).

    Multi-tenant: non-operator chats fail closed to ``satellite`` (safer UI than
    briefly showing onboard/sandbox if registry lookup fails).
    """
    cid = str(chat_id if chat_id is not None else (current_chat_id() or "")).strip()
    try:
        from core.tenant_context import DEFAULT_TENANT, multi_tenant_enabled

        if not multi_tenant_enabled():
            return "operator"

        if _is_operator_chat(cid):
            return "operator"

        tid = (tenant_id or "").strip()
        if not tid and cid:
            from storage.tenant_registry import find_tenant_by_owner_chat_id

            doc = find_tenant_by_owner_chat_id(cid) or {}
            tid = str(doc.get("tenant_id") or "").strip()
        if tid and tid != DEFAULT_TENANT:
            return "satellite"
        # Unknown or default-linked non-operator chat: slim menu (fail closed).
        if cid and not _is_operator_chat(cid):
            return "satellite"
    except Exception:
        if cid and not _is_operator_chat(cid):
            return "satellite"
    return "operator"


def _register_chat_commands_safe(chat_id: str | int | None, lang: str | None = None) -> None:
    if chat_id is None:
        return
    try:
        from notifications.telegram_commands.command_menu import register_commands_for_chat

        register_commands_for_chat(chat_id, lang=lang)
    except Exception as e:
        try:
            from logger import log

            log(f"register_commands_for_chat failed (chat={chat_id}): {e}", "WARNING")
        except Exception:
            pass


def menu_sections_for(
    *,
    chat_id: str | int | None = None,
    tenant_id: str | None = None,
    role: str | None = None,
) -> list[tuple[str, list[str]]]:
    r = role or menu_role_for(chat_id=chat_id, tenant_id=tenant_id)
    if r == "satellite":
        return MENU_SECTIONS_SATELLITE
    return MENU_SECTIONS_OPERATOR


def section_keys_for(
    section_id: str,
    *,
    chat_id: str | int | None = None,
    tenant_id: str | None = None,
) -> list[str]:
    for sid, keys in menu_sections_for(chat_id=chat_id, tenant_id=tenant_id):
        if sid == section_id:
            return list(keys)
    return []


def all_menu_command_keys() -> list[str]:
    return list(_ALL_COMMAND_KEYS)


def command_dispatch_text(key: str) -> str:
    """Telegram text for a menu key (supports multi-word commands)."""
    return _COMMAND_DISPATCH.get(key, f"/{key}")


def _reply_keyboard_enabled() -> bool:
    try:
        from core.config import get_bot_config

        return bool(get_bot_config().telegram_command_menu_config.get("reply_keyboard", True))
    except Exception:
        return True


def _main_reply_rows(
    lang: str | None = None,
    *,
    chat_id: str | int | None = None,
) -> list[list[str]]:
    rows: list[list[str]] = []
    row: list[str] = []
    for section_id, _ in menu_sections_for(chat_id=chat_id):
        row.append(section_title(section_id, lang))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def _section_reply_rows(
    section_id: str,
    lang: str | None = None,
    *,
    chat_id: str | int | None = None,
) -> list[list[str]]:
    keys = section_keys_for(section_id, chat_id=chat_id)
    rows: list[list[str]] = []
    row: list[str] = []
    for key in keys:
        row.append(command_dispatch_text(key))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([help_label(lang), back_label(lang)])
    return rows


def send_section_help(section_id: str, lang: str | None = None, chat_id=None) -> bool:
    keys = section_keys_for(section_id, chat_id=chat_id)
    if not keys:
        return False
    lang = lang or current_language()
    return send_telegram_message(
        build_section_help_message(section_id, lang, command_keys=keys)
    )


def _target_chat_id(chat_id: str | int | None = None) -> str | int | None:
    if chat_id is not None:
        return chat_id
    cid = current_chat_id()
    return cid or None


def send_main_section_keyboard(
    text: str | None = None,
    lang: str | None = None,
    *,
    chat_id: str | int | None = None,
) -> bool:
    if not _reply_keyboard_enabled():
        return False
    lang = lang or current_language()
    target = _target_chat_id(chat_id)
    return send_reply_keyboard(
        text or home_intro(lang),
        _main_reply_rows(lang, chat_id=target),
        chat_id=target,
    )


def send_section_keyboard(section_id: str, lang: str | None = None, chat_id=None) -> bool:
    target = _target_chat_id(chat_id)
    keys = section_keys_for(section_id, chat_id=target)
    if not keys:
        return False
    lang = lang or current_language()
    if target is not None:
        set_active_section(target, section_id)
    return send_reply_keyboard(
        f"<b>{section_title(section_id, lang)}</b>\n\n{section_pick(lang)}",
        _section_reply_rows(section_id, lang, chat_id=target),
        chat_id=target,
    )


def open_menu_for_chat(chat_id: str | int, lang: str | None = None) -> bool:
    """Push section reply keyboard + inline overview to a specific chat."""
    return show_home(chat_id=int(chat_id), lang=lang)


def _command_label(key: str, lang: str | None = None) -> str:
    desc = command_description(key, lang)
    if len(desc) > 28:
        desc = desc[:25] + "…"
    return desc


def _home_text(lang: str | None = None) -> str:
    return home_inline(lang)


def _section_text(
    section_id: str,
    lang: str | None = None,
    *,
    chat_id: str | int | None = None,
) -> str:
    lang = lang or current_language()
    lines = [f"<b>{section_title(section_id, lang)}</b>", "", section_pick(lang)]
    for key in section_keys_for(section_id, chat_id=chat_id):
        lines.append(f"• <code>{command_dispatch_text(key)}</code> — {_command_label(key, lang)}")
    return "\n".join(lines)


def _home_keyboard(
    lang: str | None = None,
    *,
    chat_id: str | int | None = None,
) -> list[list[dict]]:
    rows: list[list[dict]] = []
    row: list[dict] = []
    for section_id, _ in menu_sections_for(chat_id=chat_id):
        row.append({"text": section_title(section_id, lang), "callback_data": f"menu:sec:{section_id}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def _section_keyboard(
    section_id: str,
    lang: str | None = None,
    *,
    chat_id: str | int | None = None,
) -> list[list[dict]]:
    rows: list[list[dict]] = []
    row: list[dict] = []
    for key in section_keys_for(section_id, chat_id=chat_id):
        row.append({"text": _command_label(key, lang), "callback_data": f"menu:run:{key}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": back_label(lang), "callback_data": "menu:home"}])
    return rows


def show_home(*, chat_id: int | None = None, message_id: int | None = None, lang: str | None = None) -> bool:
    lang = lang or current_language()
    target = _target_chat_id(chat_id)
    # Keep chat-scoped ☰ list in sync for /menu, /start, and inline home.
    _register_chat_commands_safe(target, lang=lang)
    markup = _home_keyboard(lang, chat_id=target)
    if chat_id is not None and message_id is not None:
        return edit_telegram_message(_home_text(lang), chat_id, message_id, reply_markup=markup)
    send_main_section_keyboard(lang=lang, chat_id=target)
    return send_telegram_buttons(_home_text(lang), markup, chat_id=target)


def show_section(section_id: str, chat_id: int, message_id: int, lang: str | None = None) -> bool:
    if not section_keys_for(section_id, chat_id=chat_id):
        return show_home(chat_id=chat_id, message_id=message_id, lang=lang)
    lang = lang or current_language()
    return edit_telegram_message(
        _section_text(section_id, lang, chat_id=chat_id),
        chat_id,
        message_id,
        reply_markup=_section_keyboard(section_id, lang, chat_id=chat_id),
    )


def handle_text(text: str, chat_id=None) -> bool:
    if not _reply_keyboard_enabled():
        return False
    stripped = (text or "").strip()
    if is_back_label(stripped):
        if chat_id is not None:
            clear_context(chat_id)
            clear_active_section(chat_id)
        send_main_section_keyboard(chat_id=_target_chat_id(chat_id))
        return True
    if is_help_label(stripped):
        section_id = None
        if chat_id is not None:
            from notifications.telegram_commands.command_context import get_active_section

            section_id = get_active_section(chat_id)
        if section_id:
            send_section_help(section_id, chat_id=chat_id)
            return True
        send_telegram_message(
            "<b>❓ Hilfe</b>\n\nWähle zuerst einen Bereich — dann <i>❓ Hilfe</i> für Details zu allen Befehlen dort."
            if current_language() == "de"
            else "<b>❓ Help</b>\n\nPick a section first — then tap <i>❓ Help</i> for details on all commands there."
        )
        return True
    section_id = title_to_section_id(stripped)
    if section_id:
        allowed = {sid for sid, _ in menu_sections_for(chat_id=chat_id)}
        if section_id not in allowed:
            return False
        send_section_keyboard(section_id, chat_id=chat_id)
        return True
    return False


def handle(text: str) -> bool:
    if text not in ("/menu", "/menü"):
        return False
    cid = current_chat_id()
    show_home(chat_id=int(cid) if cid else None)
    return True


def handle_callback(callback_query: dict) -> bool:
    data = (callback_query.get("data") or "").strip()
    if not data.startswith("menu:"):
        return False

    callback_id = callback_query.get("id")
    message = callback_query.get("message") or {}
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")

    if data == "menu:home":
        if callback_id:
            answer_callback_query(callback_id)
        if chat_id and message_id:
            show_home(chat_id=chat_id, message_id=message_id)
        else:
            send_main_section_keyboard(chat_id=chat_id)
        return True

    if data.startswith("menu:sec:"):
        section_id = data.split(":", 2)[2]
        if callback_id:
            answer_callback_query(callback_id)
        if chat_id and message_id:
            show_section(section_id, chat_id, message_id)
        elif section_keys_for(section_id, chat_id=chat_id):
            send_section_keyboard(section_id, chat_id=chat_id)
        return True

    if data.startswith("menu:run:"):
        cmd_key = data.split(":", 2)[2]
        if cmd_key not in _ALL_COMMAND_KEYS:
            if callback_id:
                answer_callback_query(callback_id, callback_unknown_command())
            return True
        # Satellites: ignore operator-only menu runs (stale inline buttons).
        if chat_id is not None and menu_role_for(chat_id=chat_id) == "satellite":
            if cmd_key not in _SATELLITE_COMMAND_KEYS:
                if callback_id:
                    answer_callback_query(callback_id, callback_unknown_command())
                return True
        cmd_text = command_dispatch_text(cmd_key)
        if callback_id:
            answer_callback_query(callback_id, cmd_text)
        from notifications.telegram_commands.command_context import clear_context, set_chat_id
        from notifications.telegram_commands.router import dispatch_command

        if chat_id:
            set_chat_id(chat_id)
            clear_context(chat_id)
        dispatch_command(cmd_text)
        return True

    return False
