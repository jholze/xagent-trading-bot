from core.config import get_bot_config
from data_manager import list_coins
from notifications.telegram_commands.utils import safe_float, safe_int
from price_fetcher import get_prices
from services.trading_service import TradingService
from strategies.positions import get_position, list_active_positions
from telegram_notifier import send_telegram_message

_trading = TradingService()


def handle(text: str) -> bool:
    if text.startswith("/buy "):
        parts = [p.strip() for p in text.split() if p.strip()]
        coins = list_coins()
        if len(parts) < 2:
            send_telegram_message("❌ Usage: /buy SYMBOL USDT or /buy NUMBER USDT\nExample: /buy ARIA 200 or /buy 1 200")
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
            send_telegram_message("❌ Please specify a coin or number.\nExample: /buy ARIA 200 or /buy 1 200")
            return True

        price = get_prices(sym)[0]
        if price and price > 0:
            _trading.refresh()
            result = _trading.execute_buy(sym, "4h", price, usdt)
            mode = _trading.adapter.mode
            if result.executed:
                send_telegram_message(
                    f"✅ {mode.upper()} BUY executed: {sym} ${usdt:.0f} @ ${price:.4f}"
                    + (f"\n{result.message}" if result.message else "")
                )
            else:
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
            msg = "<b>📍 Active Positions to Sell:</b>\n\n"
            msg += "──────────────────────────────────\n"
            for i, p in enumerate(active, 1):
                highlight = p.get("highlight", "")
                price = get_prices(p["symbol"] + "/USDT" if "/" not in p["symbol"] else p["symbol"])[0]
                entry = p.get("average_entry", p.get("entry_price", 0))
                unreal = (price - entry) * p["amount"] if entry > 0 and price > 0 else 0
                msg += f"{i}. {highlight}{p['symbol']} | Amt: {p['amount']:.4f} | Entry: ${entry:.4f} | Unreal: ${unreal:.1f}\n"
                msg += "──────────────────────────────────\n"
            msg += "\nUse <code>/sell NUMBER PERCENT</code> (e.g. /sell 1 30)"
            send_telegram_message(msg)
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
                    mode = _trading.adapter.mode
                    if result.executed:
                        send_telegram_message(
                            f"✅ {mode.upper()} SELL {pct*100:.0f}% of {sym}: "
                            f"${result.usdt_amount:.0f} (PnL: ${result.pnl:.1f})"
                        )
                    else:
                        send_telegram_message(f"❌ Sell failed: {result.message}")
                    return True
        send_telegram_message("❌ Invalid selection. First run /sell to list positions.")
        return True

    return False