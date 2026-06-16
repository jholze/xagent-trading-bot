from logger import log
from notifications.telegram_commands import backtest_commands, cmc_commands, decisions_commands, gate_commands, help_commands, hermes_commands, lc_commands, menu_commands, mode_commands, order_commands, portfolio_commands, risk_commands, sandbox_commands, trading_commands, watchlist_commands, x_commands
from notifications.telegram_commands.usage_hints import hint
from telegram_notifier import send_telegram_message

_HANDLERS = [
    mode_commands.handle,
    gate_commands.handle,
    risk_commands.handle,
    sandbox_commands.handle,
    hermes_commands.handle,
    decisions_commands.handle,
    backtest_commands.handle,
    cmc_commands.handle,
    lc_commands.handle,
    watchlist_commands.handle,
    trading_commands.handle,
    order_commands.handle,
    x_commands.handle,
    portfolio_commands.handle,
    menu_commands.handle,
    help_commands.handle,
]


def dispatch_command(text: str) -> bool:
    if not isinstance(text, str):
        return False
    text = text.strip()
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
            send_telegram_message("❌ Interner Fehler beim Verarbeiten des Befehls.")
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