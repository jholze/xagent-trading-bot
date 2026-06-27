import threading

from notifications.telegram_commands.command_context import current_chat_id
from notifications.telegram_commands.position_display import send_positions_snapshot
from telegram_notifier import send_telegram_message


def _build_positions(chat_id: str):
    try:
        send_positions_snapshot(fast=True, chat_id=chat_id or None)
    except Exception as e:
        send_telegram_message(
            f"❌ Positionen konnten nicht geladen werden: {e}",
            chat_id=chat_id or None,
        )


def handle(text: str) -> bool:
    if text not in ["/positions", "/portfolio", "/status", "/balance"]:
        return False

    chat_id = current_chat_id()
    send_telegram_message("⏳ <b>Positionen</b> werden geladen…", chat_id=chat_id or None)
    threading.Thread(
        target=_build_positions,
        args=(chat_id,),
        daemon=True,
        name="positions-cmd",
    ).start()
    return True