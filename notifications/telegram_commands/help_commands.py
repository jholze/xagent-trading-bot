from notifications.telegram_commands.usage_hints import build_help_message
from telegram_notifier import send_telegram_message


def handle(text: str) -> bool:
    if text not in ["/help", "/commands", "/?"]:
        return False

    send_telegram_message(build_help_message())
    return True