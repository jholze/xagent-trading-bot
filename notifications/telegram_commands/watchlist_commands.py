from data_manager import add_coin, list_coins, remove_coin
from notifications.telegram_commands.usage_hints import hint
from notifications.telegram_commands.utils import safe_int
from telegram_notifier import send_telegram_message


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
        coins = list_coins()
        if not coins:
            send_telegram_message("📋 Watchlist ist leer.")
        else:
            msg = "📋 <b>Aktive Watchlist:</b>\n\n"
            for i, coin in enumerate(coins, 1):
                name = coin.get("name", "")
                suffix = f" ({name})" if name else ""
                msg += f"<b>{i}.</b> <b>{coin['symbol']}</b>{suffix}\n"
            send_telegram_message(msg)
        return True

    return False