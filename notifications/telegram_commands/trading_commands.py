from core.config import get_bot_config
from data_manager import list_coins
from notifications.telegram_commands.usage_hints import hint
from notifications.telegram_commands.utils import safe_float, safe_int
from price_fetcher import get_prices, get_prices_batch
from services.trading_service import TradingService
from notifications.telegram_commands.position_display import format_sell_list_message, position_symbol
from strategies.positions import get_position, list_active_positions
from telegram_notifier import send_telegram_message

# Portfolio snapshot after manual buy/sell is sent by TradingService.execute_order.

_trading = TradingService()


def handle(text: str) -> bool:
    if text == "/buy":
        send_telegram_message(hint("buy"))
        return True

    if text.startswith("/buy "):
        parts = [p.strip() for p in text.split() if p.strip()]
        coins = list_coins()
        if len(parts) < 2:
            send_telegram_message(hint("buy"))
            return True

        sym = None
        if parts[1].replace(".", "").isdigit():
            idx = safe_int(parts[1]) - 1
            if 0 <= idx < len(coins):
                sym = coins[idx]["symbol"]
            else:
                send_telegram_message("❌ Invalid coin number. First run /list to see available coins.")
                return True
        else:
            sym = (parts[1].upper() + "/USDT") if len(parts) > 1 else None

        usdt = safe_float(parts[2]) if len(parts) > 2 else get_bot_config().max_usdt_per_trade
        if not sym or usdt is None or usdt <= 0:
            send_telegram_message(hint("buy"))
            return True

        price = get_prices(sym)[0]
        if price and price > 0:
            _trading.refresh()
            result = _trading.execute_buy(sym, "4h", price, usdt)
            if not result.executed:
                send_telegram_message(f"❌ Buy failed: {result.message}")
        else:
            send_telegram_message(f"❌ Could not fetch price for {sym}. Check if the coin is valid and listed.")
        return True

    if text.startswith("/sell"):
        parts = [p.strip() for p in text.split() if p.strip()]
        if len(parts) == 1:
            active = list_active_positions()
            if not active:
                send_telegram_message("❌ No active positions to sell.")
                return True
            symbols = [position_symbol(p) for p in active]
            prices = get_prices_batch(symbols)
            send_telegram_message(format_sell_list_message(active, prices))
            return True

        idx = safe_int(parts[1]) - 1
        pct = safe_float(parts[2]) / 100 if len(parts) > 2 else 0.5
        if idx is None or pct is None or pct <= 0 or pct > 1:
            send_telegram_message("❌ Invalid number. Usage: /sell NUMBER PERCENT (e.g. /sell 1 30)")
            return True

        active = list_active_positions()
        if 0 <= idx < len(active):
            p = active[idx]
            sym = p["symbol"] + "/USDT" if "/" not in p["symbol"] else p["symbol"]
            price = get_prices(sym)[0]
            if price > 0:
                pos = get_position(sym, "4h")
                amount_sold = float(pos.get("amount", 0)) * pct
                if amount_sold > 0:
                    _trading.refresh()
                    result = _trading.execute_sell(sym, "4h", price, "SELL", amount_sold)
                    if not result.executed:
                        send_telegram_message(f"❌ Sell failed: {result.message}")
                    return True
        send_telegram_message("❌ Invalid selection. First run /sell to list positions.")
        return True

    return False