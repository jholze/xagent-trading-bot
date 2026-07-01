import threading

from notifications.telegram_commands.command_context import current_chat_id
from notifications.telegram_commands.position_display import send_positions_snapshot
from telegram_notifier import send_telegram_message

_COMPACT_COMMANDS = {"/positions", "/portfolio", "/status", "/balance"}
_FULL_COMMANDS = {
    "/positions full",
    "/positions detail",
    "/positions_full",
    "/portfolio full",
    "/portfolio detail",
}


def _build_positions(chat_id: str, *, detail_level: str):
    try:
        send_positions_snapshot(
            fast=True,
            chat_id=chat_id or None,
            detail_level=detail_level,
        )
    except Exception as e:
        send_telegram_message(
            f"❌ Positionen konnten nicht geladen werden: {e}",
            chat_id=chat_id or None,
        )


def handle(text: str) -> bool:
    if text in _COMPACT_COMMANDS:
        detail_level = "compact"
        loading = "⏳ <b>Portfolio</b> (Kurzliste) wird geladen…"
    elif text in _FULL_COMMANDS:
        detail_level = "full"
        loading = "⏳ <b>Portfolio</b> (Details) wird geladen…"
    else:
        return False

    chat_id = current_chat_id()
    send_telegram_message(loading, chat_id=chat_id or None)
    threading.Thread(
        target=_build_positions,
        args=(chat_id,),
        kwargs={"detail_level": detail_level},
        daemon=True,
        name="positions-cmd",
    ).start()
    return True