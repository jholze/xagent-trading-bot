from logger import log
from notifications.telegram_commands import ask_commands, backtest_commands, cmc_commands, decisions_commands, diag_commands, gate_commands, grid_commands, help_commands, hermes_commands, lc_commands, lock_commands, menu_commands, mode_commands, morning_commands, onboarding_commands, order_commands, plan_commands, portfolio_commands, reload_commands, replay_commands, risk_commands, sandbox_commands, short_commands, stack_commands, tenant_link_commands, trading_commands, watchlist_commands, x_commands
from notifications.telegram_commands.usage_hints import hint
from telegram_notifier import send_telegram_message

_HANDLERS = [
    tenant_link_commands.handle,
    onboarding_commands.handle,
    mode_commands.handle,
    reload_commands.handle,
    plan_commands.handle,
    gate_commands.handle,
    risk_commands.handle,
    lock_commands.handle,
    short_commands.handle,
    sandbox_commands.handle,
    hermes_commands.handle,
    ask_commands.handle,
    decisions_commands.handle,
    grid_commands.handle,
    backtest_commands.handle,
    replay_commands.handle,
    cmc_commands.handle,
    lc_commands.handle,
    watchlist_commands.handle,
    trading_commands.handle,
    order_commands.handle,
    x_commands.handle,
    diag_commands.handle,
    portfolio_commands.handle,
    morning_commands.handle,
    stack_commands.handle,
    menu_commands.handle,
    help_commands.handle,
]


def _strip_bot_suffix(text: str) -> str:
    """Telegram may send `/short@BotName` from the slash picker."""
    if not text.startswith("/"):
        return text
    head, sep, tail = text.partition(" ")
    if "@" in head:
        head = head.split("@", 1)[0]
    return f"{head} {tail}".strip() if sep else head


def dispatch_command(text: str) -> bool:
    if not isinstance(text, str):
        return False
    text = _strip_bot_suffix(text.strip())
    log(f"[DEBUG] Empfangener Befehl: '{text}'", "DEBUG")

    try:
        for handler in _HANDLERS:
            if handler(text):
                return True
        if text.startswith("/"):
            send_telegram_message(hint("unknown"))
            return True
        return False
    except Exception as e:
        log(f"Error in dispatch_command for '{text}': {e}", "ERROR")
        try:
            from notifications.telegram_i18n import t

            send_telegram_message(t("error_command"))
        except Exception:
            pass
        return True


def dispatch_callback(callback_query: dict) -> bool:
    try:
        if menu_commands.handle_callback(callback_query):
            return True
        if trading_commands.handle_callback(callback_query):
            return True
        if order_commands.handle_callback(callback_query):
            return True
        return x_commands.handle_callback(callback_query)
    except Exception as e:
        log(f"Error in dispatch_callback: {e}", "ERROR")
        return True