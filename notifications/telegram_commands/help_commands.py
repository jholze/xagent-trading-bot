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
/risk - Risk limits, drawdown, and sizing status
/mode - Show trading mode (paper/gate_testnet/live/off)
/mode paper|gate_testnet|live|off - Switch mode
/live_confirm /live_cancel - Live mainnet safety
/gate - Gate.io mainnet + testnet status
/buy SYMBOL USDT or /buy NUMBER USDT - Buy (local paper, testnet, or live)
/sell NUMBER PERCENT - Sell from position (first /sell to list)
/positions or /status - Portfolio overview with PnL and trades

<b>Sandbox (strategy experiments):</b>
/sandbox - List hypotheses being paper-tested
/sandbox_results ID - Detailed metrics for a hypothesis
/sandbox_promote ID - Promote successful hypothesis to active strategy

<b>CMC Community:</b>
/cmc or /cmcsignals - CoinMarketCap community sentiment signals

<b>X Accounts:</b>
/addx ACCOUNT - Add X account (e.g. /addx CryptoCapo_)
/removex ACCOUNT - Remove X account
/listx - List monitored X accounts
/xsignals - Show latest parsed X signals
/xaccuracy - X account accuracy leaderboard
/xposts - Show tracked posts and recommendations
/tracktest - Test tracking with a sample post

Send /help anytime for this list.
"""
    send_telegram_message(msg)
    return True