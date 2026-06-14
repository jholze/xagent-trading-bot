"""Categorized /menu hub with inline keyboards (Option A navigation)."""

from __future__ import annotations

from notifications.telegram_commands.usage_hints import USAGE
from telegram_notifier import answer_callback_query, edit_telegram_message, send_telegram_buttons

MENU_SECTIONS: list[tuple[str, str, list[str]]] = [
    ("watchlist", "📋 Watchlist", ["list", "add", "remove"]),
    ("handel", "💰 Handel", ["positions", "buy", "sell", "orders", "risk"]),
    ("modus", "⚙️ Modus & Gate", ["mode", "gate", "dryrun", "maxpositions", "live_confirm", "live_cancel"]),
    ("transparenz", "🔍 Transparenz", ["decisions", "why", "hermes", "hermes_last", "cmc"]),
    ("x", "🐦 X / Twitter", ["listx", "xsignals", "xposts", "addx", "removex", "xaccuracy"]),
    ("tests", "🧪 Tests & Lernen", ["sandbox", "backtest", "hermes_run", "testaccount"]),
]

_SECTION_BY_ID = {sid: (title, keys) for sid, title, keys in MENU_SECTIONS}


def _command_label(key: str) -> str:
    desc = USAGE.get(key, {}).get("menu_description", key)
    short = desc.split("—")[0].split("(")[0].strip()
    if len(short) > 28:
        short = short[:25] + "…"
    return short or key


def _home_text() -> str:
    return (
        "<b>🗂️ Bot-Menü</b>\n\n"
        "Wähle einen <b>Bereich</b> — darin findest du die passenden Befehle.\n"
        "<i>Schnellzugriff: positions, buy, sell direkt im Menü-Button.</i>"
    )


def _section_text(section_id: str) -> str:
    title, keys = _SECTION_BY_ID[section_id]
    lines = [f"<b>{title}</b>", "", "Wähle einen Befehl:"]
    for key in keys:
        lines.append(f"• <code>/{key}</code> — {_command_label(key)}")
    return "\n".join(lines)


def _home_keyboard() -> list[list[dict]]:
    rows: list[list[dict]] = []
    row: list[dict] = []
    for section_id, title, _ in MENU_SECTIONS:
        row.append({"text": title, "callback_data": f"menu:sec:{section_id}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def _section_keyboard(section_id: str) -> list[list[dict]]:
    _, keys = _SECTION_BY_ID[section_id]
    rows: list[list[dict]] = []
    row: list[dict] = []
    for key in keys:
        row.append({
            "text": _command_label(key),
            "callback_data": f"menu:run:{key}",
        })
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "◀ Bereiche", "callback_data": "menu:home"}])
    return rows


def show_home(*, chat_id: int | None = None, message_id: int | None = None) -> bool:
    markup = _home_keyboard()
    if chat_id is not None and message_id is not None:
        return edit_telegram_message(_home_text(), chat_id, message_id, reply_markup=markup)
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
        return True

    if data.startswith("menu:sec:"):
        section_id = data.split(":", 2)[2]
        if callback_id:
            answer_callback_query(callback_id)
        if chat_id and message_id:
            show_section(section_id, chat_id, message_id)
        return True

    if data.startswith("menu:run:"):
        cmd_key = data.split(":", 2)[2]
        allowed = {k for _, _, keys in MENU_SECTIONS for k in keys}
        if cmd_key not in allowed:
            if callback_id:
                answer_callback_query(callback_id, "Unbekannter Befehl")
            return True
        if callback_id:
            answer_callback_query(callback_id, f"/{cmd_key}")
        from notifications.telegram_commands.router import dispatch_command

        dispatch_command(f"/{cmd_key}")
        return True

    return False