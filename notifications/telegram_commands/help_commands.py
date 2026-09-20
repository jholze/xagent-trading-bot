from notifications.telegram_commands.command_context import current_chat_id
from notifications.telegram_commands.menu_i18n import (
    build_help_message,
    build_onboarding_help_message,
)
from telegram_notifier import send_telegram_message


def handle(text: str) -> bool:
    t = (text or "").strip().lower()
    # #449: explicit catalog. Satellite still role-filtered (#398) — never the
    # operator zoo. Operator /help all matches the default operator catalog.
    if t in ["/help all", "/commands all", "/? all"]:
        send_telegram_message(build_help_message(chat_id=current_chat_id(), catalog=True))
        return True

    if text in ["/help", "/commands", "/?"]:
        # #398/#449: satellite default is short “was willst du tun?”;
        # operator default remains the full catalog.
        send_telegram_message(build_help_message(chat_id=current_chat_id()))
        return True

    # Support /help onboarding (and aliases) so the operator can quickly recall
    # the full onboarding documentation directly inside Telegram.
    if t in ["/help onboarding", "/help onboard", "/help onb", "/commands onboarding", "/? onboarding"]:
        send_telegram_message(build_onboarding_help_message())
        return True

    return False