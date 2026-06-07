from telegram_notifier import send_telegram_message


def handle(text: str) -> bool:
    if text not in ["/help", "/commands", "/?"]:
        return False

    msg = """<b>🛠️ Available Commands:</b>

<b>Watchlist:</b>
/add SYMBOL - Add coin (e.g. /add RAVE)
/remove NUMBER - Remove by number (first /list)
/list or /watchlist - Show all coins

<b>Trading:</b>
/buy SYMBOL USDT or /buy NUMBER USDT - Virtual buy (e.g. /buy ARIA 200 or /buy 1 200)
/sell NUMBER PERCENT - Sell from position (first /sell to list)
/positions or /status - Portfolio overview with PnL and trades

<b>X Accounts:</b>
/addx ACCOUNT - Add X account (e.g. /addx CryptoCapo_)
/removex ACCOUNT - Remove X account
/listx - List monitored X accounts
/xsignals - Show latest parsed X signals
/xposts - Show tracked posts and recommendations
/tracktest - Test tracking with a sample post

Send /help anytime for this list.
"""
    send_telegram_message(msg)
    return True