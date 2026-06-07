from logger import log
from notifications.telegram_commands import help_commands, mode_commands, portfolio_commands, risk_commands, trading_commands, watchlist_commands, x_commands
from telegram_notifier import send_telegram_message

_HANDLERS = [
    mode_commands.handle,
    risk_commands.handle,
    watchlist_commands.handle,
    trading_commands.handle,
    x_commands.handle,
    portfolio_commands.handle,
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
        return False
    except Exception as e:
        log(f"Error in dispatch_command for '{text}': {e}", "ERROR")
        try:
            send_telegram_message("❌ Interner Fehler beim Verarbeiten des Befehls.")
        except Exception:
            pass
        return True