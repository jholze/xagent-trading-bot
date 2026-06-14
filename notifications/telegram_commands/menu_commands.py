"""Categorized Telegram navigation: sections in native menu + reply keyboard."""

from __future__ import annotations

from notifications.telegram_commands.usage_hints import USAGE
from telegram_notifier import (
    answer_callback_query,
    edit_telegram_message,
    send_reply_keyboard,
    send_telegram_buttons,
)

# All bot commands grouped by Bereich (single source of truth).
MENU_SECTIONS: list[tuple[str, str, str, list[str]]] = [
    ("watchlist", "📋 Watchlist", "Watchlist", ["list", "add", "remove"]),
    ("handel", "💰 Handel", "Handel", ["positions", "buy", "sell", "orders", "risk"]),
    ("modus", "⚙️ Modus & Gate", "Modus", ["mode", "gate", "dryrun", "maxpositions", "live_confirm", "live_cancel"]),
    ("transparenz", "🔍 Transparenz", "Transparenz", ["decisions", "why", "hermes", "hermes_last", "cmc"]),
    ("x", "🐦 X / Twitter", "X", ["addx", "removex", "listx", "xposts", "xsignals", "xaccuracy", "tracktest", "testaccount"]),
    (
        "tests",
        "🧪 Sandbox & Backtest",
        "Tests",
        ["sandbox", "sandbox_results", "sandbox_promote", "backtest", "backtest_lock", "backtest_results", "hermes_run"],
    ),
    ("hilfe", "❓ Hilfe", "Hilfe", ["menu", "help"]),
]

BACK_LABEL = "◀ Bereiche"

_SECTION_BY_ID = {sid: (title, short, keys) for sid, title, short, keys in MENU_SECTIONS}
_TITLE_TO_SECTION = {title: sid for sid, title, _, _ in MENU_SECTIONS}
_ALL_COMMAND_KEYS = [k for _, _, _, keys in MENU_SECTIONS for k in keys]


def section_prefixed_description(section_id: str, key: str) -> str:
    _, short, _ = _SECTION_BY_ID[section_id]
    base = USAGE.get(key, {}).get("menu_description", key)
    text = f"{short} · {base}"
    return text[:256]


def all_menu_command_keys() -> list[str]:
    """Flat command list in section order for setMyCommands."""
    return list(_ALL_COMMAND_KEYS)


def _command_label(key: str) -> str:
    desc = USAGE.get(key, {}).get("menu_description", key)
    short = desc.split("—")[0].split("(")[0].strip()
    if len(short) > 28:
        short = short[:25] + "…"
    return short or key


def _reply_keyboard_enabled() -> bool:
    try:
        from core.config import get_bot_config

        return bool(get_bot_config().telegram_command_menu_config.get("reply_keyboard", True))
    except Exception:
        return True


def _main_reply_rows() -> list[list[str]]:
    rows: list[list[str]] = []
    row: list[str] = []
    for _, title, _, _ in MENU_SECTIONS:
        row.append(title)
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def _section_reply_rows(section_id: str) -> list[list[str]]:
    _, title, _, keys = next(s for s in MENU_SECTIONS if s[0] == section_id)
    rows: list[list[str]] = []
    row: list[str] = []
    for key in keys:
        row.append(f"/{key}")
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([BACK_LABEL])
    return rows


def send_main_section_keyboard(text: str | None = None) -> bool:
    if not _reply_keyboard_enabled():
        return False
    msg = text or (
        "<b>🗂️ Bereiche</b>\n\n"
        "Wähle unten einen Bereich — darunter erscheinen alle Befehle dieses Bereichs.\n"
        "<i>Im Menü-Button (☰) sind dieselben Befehle nach Bereich sortiert.</i>"
    )
    return send_reply_keyboard(msg, _main_reply_rows())


def send_section_keyboard(section_id: str) -> bool:
    if section_id not in _SECTION_BY_ID:
        return False
    title, _, _ = _SECTION_BY_ID[section_id]
    return send_reply_keyboard(
        f"<b>{title}</b>\n\nTippe einen Befehl:",
        _section_reply_rows(section_id),
    )


def _home_text() -> str:
    return (
        "<b>🗂️ Bot-Menü</b>\n\n"
        "Wähle einen <b>Bereich</b> — alle Befehle sind nach Kategorien sortiert.\n"
        "<i>Unten: Bereichs-Tastatur · Menü-Button (☰): komplette Liste mit Bereichs-Prefix.</i>"
    )


def _section_text(section_id: str) -> str:
    title, _, keys = _SECTION_BY_ID[section_id]
    lines = [f"<b>{title}</b>", "", "Wähle einen Befehl:"]
    for key in keys:
        lines.append(f"• <code>/{key}</code> — {_command_label(key)}")
    return "\n".join(lines)


def _home_keyboard() -> list[list[dict]]:
    rows: list[list[dict]] = []
    row: list[dict] = []
    for section_id, title, _, _ in MENU_SECTIONS:
        row.append({"text": title, "callback_data": f"menu:sec:{section_id}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def _section_keyboard(section_id: str) -> list[list[dict]]:
    _, _, keys = _SECTION_BY_ID[section_id]
    rows: list[list[dict]] = []
    row: list[dict] = []
    for key in keys:
        row.append({"text": _command_label(key), "callback_data": f"menu:run:{key}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": BACK_LABEL, "callback_data": "menu:home"}])
    return rows


def show_home(*, chat_id: int | None = None, message_id: int | None = None) -> bool:
    markup = _home_keyboard()
    if chat_id is not None and message_id is not None:
        return edit_telegram_message(_home_text(), chat_id, message_id, reply_markup=markup)
    send_main_section_keyboard()
    return send_telegram_buttons(_home_text(), markup)


def show_section(section_id: str, chat_id: int, message_id: int) -> bool:
    if section_id not in _SECTION_BY_ID:
        return False
    return edit_telegram_message(
        _section_text(section_id),
        chat_id,
        message_id,
        reply_markup=_section_keyboard(section_id),
    )


def handle_text(text: str) -> bool:
    """Reply-keyboard taps: section title or back."""
    if not _reply_keyboard_enabled():
        return False
    stripped = (text or "").strip()
    if stripped == BACK_LABEL:
        send_main_section_keyboard()
        return True
    section_id = _TITLE_TO_SECTION.get(stripped)
    if section_id:
        send_section_keyboard(section_id)
        return True
    return False


def handle(text: str) -> bool:
    if text not in ("/menu", "/menü"):
        return False
    show_home()
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
            send_main_section_keyboard()
        return True

    if data.startswith("menu:sec:"):
        section_id = data.split(":", 2)[2]
        if callback_id:
            answer_callback_query(callback_id)
        if chat_id and message_id:
            show_section(section_id, chat_id, message_id)
        elif section_id in _SECTION_BY_ID:
            send_section_keyboard(section_id)
        return True

    if data.startswith("menu:run:"):
        cmd_key = data.split(":", 2)[2]
        if cmd_key not in _ALL_COMMAND_KEYS:
            if callback_id:
                answer_callback_query(callback_id, "Unbekannter Befehl")
            return True
        if callback_id:
            answer_callback_query(callback_id, f"/{cmd_key}")
        from notifications.telegram_commands.router import dispatch_command

        dispatch_command(f"/{cmd_key}")
        return True

    return False