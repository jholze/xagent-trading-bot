from core.config import get_bot_config
from data_manager import (
    add_coin,
    is_dry_run_enhanced,
    is_demo_mode,
    list_coins,
    remove_coin,
    uses_watchlist_expansion,
)
from notifications.telegram_commands.usage_hints import hint
from notifications.telegram_commands.utils import safe_int
from notifications.telegram_commands.command_context import activate_command
from telegram_notifier import send_telegram_message


def _coin_symbol(coin: dict) -> str:
    sym = coin.get("symbol", "")
    return sym if "/" in sym else f"{sym}/USDT"


def _watchlist_mode_label() -> str:
    if is_demo_mode():
        return "Demo"
    if is_dry_run_enhanced():
        return "Enhanced Dry Run"
    if uses_watchlist_expansion():
        return "Dry Run"
    return ""


def format_watchlist_message(coins: list = None) -> str:
    coins = coins if coins is not None else list_coins()
    if not coins:
        return "📋 Watchlist ist leer."
    mode = _watchlist_mode_label()
    title = f"📋 <b>Watchlist</b> ({mode})" if mode else "📋 <b>Aktive Watchlist</b>"
    msg = f"{title}\n\n"
    for i, coin in enumerate(coins, 1):
        msg += _format_coin_line(i, coin) + "\n"
    return msg.rstrip()


def format_buy_list_message(coins: list, prices: dict) -> str:
    from price_fetcher import format_usdt_price

    if not coins:
        return "❌ Watchlist ist leer. Zuerst <code>/add SYMBOL</code> nutzen."
    default_usdt = get_bot_config().max_usdt_per_trade
    msg = "<b>🛒 Coins kaufen</b>\n\n"
    for i, coin in enumerate(coins, 1):
        sym = _coin_symbol(coin)
        price = float(prices.get(sym, 0) or 0)
        price_str = format_usdt_price(price)
        msg += f"{_format_coin_line(i, coin)}\n   └ Kurs <b>{price_str}</b>\n"
    from notifications.telegram_commands.menu_i18n import context_footer, current_language

    msg += "\n" + context_footer(
        "buy",
        current_language(),
        default_usdt=f"{default_usdt:.0f}",
        example=f"1 {default_usdt:.0f}",
    )
    return msg


def _format_coin_line(index: int, coin: dict, trending: bool = False) -> str:
    from notifications.coin_links import format_ticker_html

    name = coin.get("name", "")
    ticker = coin.get("symbol", "").split("/")[0]
    sym_html = format_ticker_html(ticker, name=name, symbol_suffix="/USDT")
    suffix = f" ({name})" if name else ""
    inactive = "" if coin.get("active", True) else " <i>(inaktiv)</i>"
    if trending or coin.get("source") in ("cmc_trending", "dry_run_expansion"):
        rank = coin.get("trending_rank")
        tag = f" 📈Trending #{rank}" if rank else " 📈Trending"
    else:
        tag = ""
    return f"<b>{index}.</b> <b>{sym_html}</b>{suffix}{inactive}{tag}"


def resolve_coin_by_display_index(coins: list, index: int):
    """Map 0-based display index (from /list or /buy list) to a watchlist coin."""
    if 0 <= index < len(coins):
        return coins[index]
    return None


def handle(text: str) -> bool:
    if text == "/add":
        activate_command("add")
        send_telegram_message(hint("add"))
        return True

    if text == "/remove":
        activate_command("remove")
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