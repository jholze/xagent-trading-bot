from data_manager import load_trade_history
from notifications.telegram_commands.position_display import format_positions_message, position_symbol
from price_fetcher import get_prices_batch
from services.trading_service import TradingService
from strategies.positions import list_active_positions
from telegram_notifier import send_telegram_message


def handle(text: str) -> bool:
    if text not in ["/positions", "/status", "/balance"]:
        return False

    active = list_active_positions()
    history = load_trade_history()
    symbols = [position_symbol(p) for p in active]
    prices = get_prices_batch(symbols) if symbols else {}

    mode_label = TradingService().mode_label()
    msg = format_positions_message(
        active,
        prices,
        history,
        mode_label=mode_label,
        include_trades=True,
    )
    send_telegram_message(msg)
    return True