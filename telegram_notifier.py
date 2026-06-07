import os
from datetime import datetime

import requests

from data_manager import (
    add_coin,
    get_config,
    get_text,
    is_demo_mode,
    list_coins,
    load_trade_history,
    load_watchlist,
    load_x_accounts,
    load_x_posts,
    record_trade,
    remove_coin,
    save_config,
    save_full_coin,
    save_watchlist,
    save_x_accounts,
    save_x_posts,
)
from price_fetcher import get_prices
from strategies.positions import get_position, list_active_positions, update_position
from x_analyzer import XAnalyzer
from logger import log

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

search_results = {}


def _safe_int(value: str, default: int = None) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _safe_float(value: str, default: float = None) -> float:
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def send_signal_message(
    signal,
    coin,
    current_price,
    rsi,
    lower_bb,
    vol_multiplier,
    ampel_emoji=None,
    ampel_text=None,
):
    symbol = coin.get("symbol", "Unknown")
    name = coin.get("name", symbol)

    if signal == "BUY":
        emoji = "🟢"
        title = "BUY EXECUTED"
        pos = get_position(symbol, "4h")
        amount = float(pos.get("amount", 0))
        cost = current_price * amount if current_price > 0 else 0
        extra = f"\n<b>Amount:</b> {amount:.4f} | <b>Cost:</b> ${cost:.1f}"
    elif "SELL" in signal:
        emoji = "🔴"
        pct = "20%" if "20" in signal else "30%" if "30" in signal else "STOP"
        title = f"SELL {pct} EXECUTED"
        extra = ""
    else:
        emoji = "📡"
        title = "X SIGNAL"
        extra = f"\n<b>Confidence:</b> {ampel_text}" if ampel_text else ""

    ampel_line = f"<b>Ampel:</b> {ampel_emoji} {ampel_text}\n" if ampel_emoji and ampel_emoji != "📡" else ""
    price_str = f"{current_price:.4f}" if isinstance(current_price, (int, float)) and current_price > 0 else "—"
    rsi_str = f"{rsi:.1f}" if isinstance(rsi, (int, float)) and rsi > 0 else "—"
    lower_str = f"{lower_bb:.4f}" if isinstance(lower_bb, (int, float)) and lower_bb > 0 else "—"

    message = f"""
{emoji} <b>{title}</b> — {symbol}

<b>Name:</b> {name}
<b>Preis:</b> ${price_str}
<b>RSI:</b> {rsi_str}
{ampel_line}{extra}
🕒 {datetime.now().strftime("%H:%M:%S")}
"""
    send_telegram_message(message.strip())


def send_x_recommendation_message(recommendation):
    """Clean message for X recommendations with raw tweet and rationale."""
    emoji = "🟢" if recommendation["action"] == "BUY" else "🔴" if recommendation["action"] == "SELL" else "📋" if recommendation["action"] == "ADD_TO_WATCHLIST" else "⏸️"
    title = recommendation["action"]
    raw = recommendation.get("raw_tweet", "—")[:100] + "..." if len(recommendation.get("raw_tweet", "")) > 100 else recommendation.get("raw_tweet", "—")
    msg = f"""{emoji} <b>{title} RECOMMENDATION</b> — {recommendation.get("coin", "UNKNOWN")}/USDT

<b>From:</b> @{recommendation.get("account", "Unknown")}
<b>Raw Tweet:</b> {raw}
<b>Confidence:</b> {recommendation.get("confidence", 0)}%
<b>Rationale:</b> {recommendation.get("rationale", "—")}

🕒 {datetime.now().strftime("%H:%M:%S")}
"""
    send_telegram_message(msg)


def send_telegram_message(text):
    if not BOT_TOKEN or not CHAT_ID:
        print("⚠️ Telegram not configured")
        return False

    if is_demo_mode():
        text = "🧪 [DEMO] " + text

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}

    try:
        response = requests.post(url, json=payload, timeout=10)
        return response.status_code == 200
    except Exception as e:
        print(f"Error sending Telegram message: {e}")
        return False


def handle_telegram_command(text):
    """Robust Telegram command dispatcher with safe parsing, validation and top-level error net."""
    if not isinstance(text, str):
        return False
    text = text.strip()
    log(f"[DEBUG] Empfangener Befehl: '{text}'", "DEBUG")

    try:
        # === Command Dispatcher ===
        if text.startswith("/add "):
            query = text[5:].strip().upper()
            if not query:
                send_telegram_message("Bitte einen Coin-Namen angeben, z.B. /add RAVE")
                return True
            success, msg = add_coin(query)
            send_telegram_message(f"{'✅' if success else '❌'} {msg}")
            return True

        elif text.startswith("/remove "):
            index = _safe_int(text[8:].strip())
            if index is None:
                send_telegram_message("❌ Bitte eine Nummer angeben, z.B. /remove 2")
                return True
            coins = list_coins()
            if index < 1 or index > len(coins):
                send_telegram_message("❌ Ungültige Nummer.")
                return True
            symbol = coins[index - 1]["symbol"]
            success, msg = remove_coin(symbol)
            send_telegram_message(f"{'✅' if success else '❌'} {msg}")
            return True

        elif text in ["/list", "/watchlist", "/show"]:
            coins = list_coins()
            if not coins:
                send_telegram_message("📋 Watchlist ist leer.")
            else:
                msg = "📋 <b>Aktive Watchlist:</b>\n\n"
                for i, coin in enumerate(coins, 1):
                    msg += f"{i}. {coin['symbol']} ({coin.get('name', '')})\n"
                send_telegram_message(msg)
            return True

        elif text.startswith("/buy "):
            parts = [p.strip() for p in text.split() if p.strip()]
            coins = list_coins()
            if len(parts) < 2:
                send_telegram_message("❌ Usage: /buy SYMBOL USDT or /buy NUMBER USDT\nExample: /buy ARIA 200 or /buy 1 200")
                return True

            sym = None
            usdt = None

            # Support both "/buy 1 200" (index) and "/buy ARIA 200" (symbol)
            if parts[1].replace(".", "").isdigit():
                idx = _safe_int(parts[1]) - 1
                if 0 <= idx < len(coins):
                    sym = coins[idx]["symbol"]
                else:
                    send_telegram_message("❌ Invalid coin number. First run /list to see available coins.")
                    return True
            else:
                sym = (parts[1].upper() + "/USDT") if len(parts) > 1 else None

            usdt = _safe_float(parts[2]) if len(parts) > 2 else get_config().get("max_usdt_per_trade", 150)

            if not sym or usdt is None or usdt <= 0:
                send_telegram_message("❌ Please specify a coin or number.\nExample: /buy ARIA 200 or /buy 1 200")
                return True

            price = get_prices(sym)[0]
            if price and price > 0:
                amount = usdt / price
                update_position(sym, "4h", "BUY", price, amount)
                record_trade({"type": "BUY", "symbol": sym, "price": price, "amount": amount, "usdt_amount": usdt, "timestamp": datetime.now().isoformat()})
                send_telegram_message(f"✅ Virtual BUY executed: {sym} ${usdt:.0f} @ ${price:.4f}")
            else:
                send_telegram_message(f"❌ Could not fetch price for {sym}. Check if the coin is valid and listed.")
            return True

        elif text.startswith("/sell"):
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
            else:
                idx = _safe_int(parts[1]) - 1
                pct = _safe_float(parts[2]) / 100 if len(parts) > 2 else 0.5

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
                            received = price * amount_sold
                            pnl = (price - pos.get("average_entry", pos.get("entry_price", price))) * amount_sold
                            update_position(sym, "4h", "SELL", price, amount_sold)
                            record_trade({"type": "SELL", "symbol": sym, "price": price, "amount": amount_sold, "usdt_received": received, "pnl": pnl, "timestamp": datetime.now().isoformat()})
                            send_telegram_message(f"✅ Virtual SELL {pct*100:.0f}% of {sym}: ${received:.0f} (PnL: ${pnl:.1f})")
                            return True
                send_telegram_message("❌ Invalid selection. First run /sell to list positions.")
                return True

        elif text.startswith("/addx "):
            handle = text[6:].strip().replace("@", "").strip()
            if not handle:
                send_telegram_message("Please provide an X account, e.g. /addx CryptoCapo_")
                return True
            accounts = load_x_accounts()
            if not any(a.get("handle", a) == handle for a in accounts):
                accounts.append({"handle": handle, "trust_score": 70, "enabled": True, "notes": "Added via Telegram"})
                if save_x_accounts(accounts):
                    send_telegram_message(f"✅ Added @{handle} to monitored X accounts.")
                else:
                    send_telegram_message("❌ Failed to save x_accounts.json.")
            else:
                send_telegram_message(f"@{handle} is already monitored.")
            return True

        elif text.startswith("/removex "):
            handle = text[8:].strip().replace("@", "").strip()
            if not handle:
                send_telegram_message("Please provide an X account to remove, e.g. /removex CryptoCapo_")
                return True
            accounts = load_x_accounts()
            new_accounts = [a for a in accounts if a.get("handle", a) != handle]
            if len(new_accounts) != len(accounts):
                if save_x_accounts(new_accounts):
                    send_telegram_message(f"✅ Removed @{handle} from X accounts.")
                else:
                    send_telegram_message("❌ Failed to save x_accounts.json.")
            else:
                send_telegram_message(f"@{handle} not found.")
            return True

        elif text in ["/listx", "/xaccounts", "/xlist"]:
            accounts = load_x_accounts()
            if not accounts:
                send_telegram_message("No X accounts configured.")
            else:
                msg = "<b>📋 Monitored X Accounts:</b>\n\n"
                for a in accounts:
                    handle = a.get("handle", a)
                    trust = a.get("trust_score", 70)
                    enabled = "🟢" if a.get("enabled", True) else "🔴"
                    msg += f"{enabled} @{handle} | Trust: {trust} | {a.get('notes', '')}\n"
                send_telegram_message(msg)
            return True

        elif text in ["/xposts", "/xhistory", "/xlog"]:
            posts = load_x_posts().get("posts", [])[-10:]
            if not posts:
                send_telegram_message("No tracked X posts yet.")
            else:
                msg = "<b>📜 Last 10 Tracked X Posts:</b>\n\n"
                for p in reversed(posts):
                    ts = p.get("timestamp", "")[:16].replace("T", " ")
                    rec = p.get("action", "IGNORE")
                    emoji = "🟢" if rec == "BUY" else "🔴" if rec == "SELL" else "📋" if rec == "ADD_TO_WATCHLIST" else "⏸️"
                    raw = p.get("raw_tweet", "—")[:80] + "..." if len(p.get("raw_tweet", "")) > 80 else p.get("raw_tweet", "—")
                    msg += f"{emoji} {ts} | @{p.get('account')} | {rec} {p.get('coin')} | {p.get('confidence')}% \nRaw: {raw}\nRationale: {p.get('rationale', '')[:80]}...\n\n"
                send_telegram_message(msg)
            return True

        elif text == "/tracktest":
            from x_analyzer import XAnalyzer
            analyzer = XAnalyzer()
            test_tweet = "SOL looking very strong on the weekly chart. Breaking resistance with good volume. Long bias."
            recommendation = analyzer.track_and_recommend(test_tweet, "TestAccount", 0.05)
            analyzer.log_tracked_post(recommendation)
            send_x_recommendation_message(recommendation)
            return True

        elif text in ["/positions", "/status", "/balance"]:
            active = list_active_positions()
            history = load_trade_history()
            total_unreal = 0.0
            for p in active:
                price = get_prices(p["symbol"] + "/USDT" if "/" not in p["symbol"] else p["symbol"])[0]
                entry = p.get("average_entry", p.get("entry_price", 0))
                if price > 0 and entry > 0:
                    total_unreal += (price - entry) * p["amount"]
            total_value = history.get("virtual_balance", 0) + total_unreal
            total_pnl = history.get("realized_pnl", 0) + total_unreal
            pnl_pct = (total_pnl / 5000 * 100) if total_pnl != 0 else 0

            msg = f"""<b>📊 Portfolio Overview</b>

Balance: <b>${history.get("virtual_balance", 0):.0f}</b>
Unrealized: <b>${total_unreal:.1f}</b>
Total Value: <b>${total_value:.0f}</b>
Total PnL: <b>${total_pnl:.1f}</b> ({pnl_pct:.1f}%)

<b>Active Positions ({len(active)}):</b>
"""
            msg += "──────────────────────────────────\n"
            for p in active:
                highlight = p.get("highlight", "")
                price = get_prices(p["symbol"] + "/USDT" if "/" not in p["symbol"] else p["symbol"])[0]
                entry = p.get("average_entry", p.get("entry_price", 0))
                amount = float(p["amount"])
                unreal = (price - entry) * amount if entry > 0 and price > 0 else 0
                unreal_pct = (unreal / (entry * amount) * 100) if entry > 0 and amount > 0 else 0
                msg += f"{highlight}{p['symbol']:8} | Amt: {amount:.4f} | Entry: ${entry:.4f} | Unreal: ${unreal:.1f} ({unreal_pct:+.1f}%)\n"
                msg += "──────────────────────────────────\n"

            msg += "\n<b>── Last Trades ──</b>\n"
            msg += "──────────────────────────────────\n"
            trades = history.get("trades", [])[-8:]
            for t in reversed(trades):
                ts = t.get("timestamp", "")[:16].replace("T", " ")
                typ = "🟢 BUY" if t.get("type") == "BUY" else "🔴 SELL"
                pnl_str = f" PnL:${t.get('pnl', 0):+.1f}" if t.get("pnl") is not None else ""
                msg += f"{ts} | {typ} | {t.get('symbol',''):<10} | ${t.get('price',0):.4f} | Amt:{t.get('amount',0):.4f}{pnl_str}\n"
                msg += "──────────────────────────────────\n"

            send_telegram_message(msg)
            return True

        elif text in ["/help", "/commands", "/?"]:
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

        return False

    except Exception as e:
        log(f"Error in handle_telegram_command for '{text}': {e}", "ERROR")
        try:
            send_telegram_message("❌ Interner Fehler beim Verarbeiten des Befehls.")
        except Exception:
            pass
        return True
