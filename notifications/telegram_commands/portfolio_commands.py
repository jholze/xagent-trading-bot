import threading

from notifications.telegram_commands.position_display import send_positions_snapshot
from telegram_notifier import send_telegram_message


def _build_positions():
    try:
        send_positions_snapshot(fast=True)
    except Exception as e:
        send_telegram_message(f"❌ Positionen konnten nicht geladen werden: {e}")


def handle(text: str) -> bool:
    if text not in ["/positions", "/portfolio", "/status", "/balance"]:
        return False

    send_telegram_message("⏳ <b>Positionen</b> werden geladen…")
    threading.Thread(target=_build_positions, daemon=True, name="positions-cmd").start()
    return True