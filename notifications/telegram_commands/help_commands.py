from notifications.telegram_commands.menu_i18n import (
    build_help_message,
    build_onboarding_help_message,
)
from telegram_notifier import send_telegram_message


def handle(text: str) -> bool:
    if text in ["/help", "/commands", "/?"]:
        send_telegram_message(build_help_message())
        return True

    # Support /help onboarding (and aliases) so the operator can quickly recall
    # the full onboarding documentation directly inside Telegram.
    t = (text or "").strip().lower()
    if t in ["/help onboarding", "/help onboard", "/help onb", "/commands onboarding", "/? onboarding"]:
        send_telegram_message(build_onboarding_help_message())
        return True

    return False