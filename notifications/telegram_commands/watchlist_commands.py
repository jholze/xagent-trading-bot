from core.config import get_bot_config
from data_manager import add_coin, list_coins, remove_coin
from notifications.telegram_commands.usage_hints import hint
from notifications.telegram_commands.utils import safe_int
from telegram_notifier import send_telegram_message


def _coin_symbol(coin: dict) -> str:
    sym = coin.get("symbol", "")
    return sym if "/" in sym else f"{sym}/USDT"


def format_watchlist_message(coins: list) -> str:
    if not coins:
        return "📋 Watchlist ist leer."
    msg = "📋 <b>Aktive Watchlist:</b>\n\n"
    for i, coin in enumerate(coins, 1):
        msg += _format_coin_line(i, coin) + "\n"
    return msg.rstrip()


def format_buy_list_message(coins: list, prices: dict) -> str:
    if not coins:
        return "❌ Watchlist ist leer. Zuerst <code>/add SYMBOL</code> nutzen."
    default_usdt = get_bot_config().max_usdt_per_trade
    msg = "<b>🛒 Coins kaufen</b>\n\n"
    for i, coin in enumerate(coins, 1):
        sym = _coin_symbol(coin)
        price = float(prices.get(sym, 0) or 0)
        price_str = f"${price:.4f}" if price > 0 else "—"
        msg += f"{_format_coin_line(i, coin)}\n   └ Kurs <b>{price_str}</b>\n"
    msg += (
        f"\n<code>/buy NUMMER USDT</code>  ·  z.B. <code>/buy 1 {default_usdt:.0f}</code>\n"
        f"<i>Ohne USDT-Betrag: Standard ${default_usdt:.0f}</i>"
    )
    return msg


def _format_coin_line(index: int, coin: dict) -> str:
    name = coin.get("name", "")
    suffix = f" ({name})" if name else ""
    inactive = "" if coin.get("active", True) else " <i>(inaktiv)</i>"
    return f"<b>{index}.</b> <b>{coin['symbol']}</b>{suffix}{inactive}"


def resolve_coin_by_display_index(coins: list, index: int):
    """Map 0-based display index (from /list or /buy list) to a watchlist coin."""
    if 0 <= index < len(coins):
        return coins[index]
    return None


def handle(text: str) -> bool:
    if text == "/add":
        send_telegram_message(hint("add"))
        return True

    if text == "/remove":
        send_telegram_message(hint("remove"))
        return True

    if text.startswith("/add "):
        query = text[5:].strip().upper()
        if not query:
            send_telegram_message(hint("add"))
            return True
        success, msg = add_coin(query)
        send_telegram_message(f"{'✅' if success else '❌'} {msg}")
        return True

    if text.startswith("/remove "):
        index = safe_int(text[8:].strip())
        if index is None:
            send_telegram_message(hint("remove"))
            return True
        coins = list_coins()
        if index < 1 or index > len(coins):
            send_telegram_message("❌ Ungültige Nummer.")
            return True
        symbol = coins[index - 1]["symbol"]
        success, msg = remove_coin(symbol)
        send_telegram_message(f"{'✅' if success else '❌'} {msg}")
        return True

    if text in ["/list", "/watchlist", "/show"]:
        send_telegram_message(format_watchlist_message(list_coins()))
        return True

    return False