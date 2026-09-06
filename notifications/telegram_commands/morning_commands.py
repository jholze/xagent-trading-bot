import threading

from notifications.telegram_commands.menu_i18n import current_language, set_user_language
from notifications.telegram_i18n import t
from telegram_notifier import send_telegram_message

_cmd_threads: list[threading.Thread] = []


def _run_morning(lang: str):
    from notifications.morning_briefing import send_morning_briefing

    try:
        set_user_language(lang)
        send_morning_briefing()
    except Exception as e:
        set_user_language(lang)
        send_telegram_message(t("morning_failed", error=e))


def handle(text: str) -> bool:
    if text != "/morning":
        return False

    lang = current_language()
    send_telegram_message(t("loading_morning"))
    thread = threading.Thread(
        target=_run_morning,
        args=(lang,),
        daemon=True,
        name="morning-cmd",
    )
    _cmd_threads.append(thread)
    thread.start()
    return True


def reset_morning_commands_for_tests() -> None:
    """Join leftover /morning worker threads (pytest workers, #329)."""
    threads = list(_cmd_threads)
    _cmd_threads.clear()
    for thread in threads:
        if thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
