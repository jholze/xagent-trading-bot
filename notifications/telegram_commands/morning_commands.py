import threading

from telegram_notifier import send_telegram_message


def _run_morning():
    from notifications.morning_briefing import send_morning_briefing

    try:
        send_morning_briefing()
    except Exception as e:
        send_telegram_message(f"❌ Morning Briefing fehlgeschlagen: {e}")


def handle(text: str) -> bool:
    if text != "/morning":
        return False

    send_telegram_message("☀️ <b>Morning Briefing</b> wird erstellt…")
    threading.Thread(target=_run_morning, daemon=True, name="morning-cmd").start()
    return True