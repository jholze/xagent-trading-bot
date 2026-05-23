import os
from datetime import datetime

import requests

from data_manager import (
    add_coin,
    get_text,
    list_coins,
    load_config,
    load_trade_history,
    load_watchlist,
    load_x_accounts,
    record_trade,
    remove_coin,
    save_config,
    save_full_coin,
    save_watchlist,
    save_x_accounts,
)
from price_fetcher import get_prices
from strategies.positions import get_position, list_active_positions, update_position

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Temporärer Speicher für Suchergebnisse (pro Chat)
search_results = {}


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



def send_telegram_message(text):
    if not BOT_TOKEN or not CHAT_ID:
        print("⚠️ Telegram nicht konfiguriert")
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}

    try:
        response = requests.post(url, json=payload, timeout=10)
        return response.status_code == 200
    except Exception as e:
        print(f"Fehler beim Senden: {e}")
        return False


def handle_telegram_command(text):
    text = text.strip()
    print(f"[DEBUG] Empfangener Befehl: '{text}'")

    # Smart Add
    if text.startswith("/add "):
        query = text[5:].strip().upper()
        if not query:
            send_telegram_message("Bitte einen Coin-Namen angeben, z.B. /add RAVE")
            return True

        # Hier später echte Suche einbauen. Für den Anfang einfaches Add
        success, msg = add_coin(query)
        send_telegram_message(f"{'✅' if success else '❌'} {msg}")
        return True

    elif text.startswith("/select "):
        try:
            index = int(text[8:].strip())
            if index in search_results:
                coin_data = search_results[index]
                success, msg = save_full_coin(coin_data)
                send_telegram_message(f"{'✅' if success else '❌'} {msg}")
                del search_results[index]  # Aufräumen
            else:
                send_telegram_message("❌ Ungültige Auswahl.")
            return True
        except:
            send_telegram_message("❌ Bitte eine Nummer angeben, z.B. /select 2")
            return True

    elif text.startswith("/remove "):
        try:
            index = int(text[8:].strip())
            coins = list_coins()
            if index < 1 or index > len(coins):
                send_telegram_message("❌ Ungültige Nummer.")
                return True
            symbol = coins[index - 1]["symbol"]
            success, msg = remove_coin(symbol)
            send_telegram_message(f"{'✅' if success else '❌'} {msg}")
            return True
        except:
            send_telegram_message("❌ Bitte eine Nummer angeben, z.B. /remove 2")
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
        parts = text.split()
        coins = list_coins()

        if len(parts) < 2:
            send_telegram_message("❌ Usage: /buy SYMBOL USDT or /buy NUMBER USDT\nExample: /buy ARIA 200 or /buy 1 200")
            return True

        # Check if first argument is index (number)
        if parts[1].replace(".", "").isdigit():
            idx = int(parts[1]) - 1
            if 0 <= idx < len(coins):
                sym = coins[idx]["symbol"]
                usdt = float(parts[2]) if len(parts) > 2 else load_config().get("max_usdt_per_trade", 150)
            else:
                send_telegram_message("❌ Invalid coin number. First run /list to see available coins (1-based index).")
                return True
        else:
            sym = (parts[1].upper() + "/USDT") if len(parts) > 1 else None
            usdt = float(parts[2]) if len(parts) > 2 else load_config().get("max_usdt_per_trade", 150)

        if not sym:
            send_telegram_message("❌ Please specify a coin or valid number.\nExample: /buy ARIA 200 or /buy 1 200")
            return True

        price = get_prices(sym)[0]
        if price and price > 0:
            amount = usdt / price
            record_trade({"type": "BUY", "symbol": sym, "price": price, "amount": amount, "usdt_amount": usdt, "timestamp": datetime.now().isoformat()})
            update_position(sym, "4h", "BUY", price, amount)
            send_telegram_message(f"✅ Virtual BUY executed: {sym} ${usdt:.0f} @ ${price:.4f}")
        else:
            send_telegram_message(f"❌ Could not fetch price for {sym}. Check if the coin is valid and listed.")
        return True


        # Check if first argument is index (number)
        if parts[1].replace(".", "").isdigit():
            idx = int(parts[1]) - 1
            if 0 <= idx < len(coins):
                sym = coins[idx]["symbol"]
                usdt = float(parts[2]) if len(parts) > 2 else load_config().get("max_usdt_per_trade", 150)
            else:
                send_telegram_message("❌ Invalid coin number. First run /list to see available coins.")
                return True
        else:
            sym = (parts[1].upper() + "/USDT") if len(parts) > 1 else None
            usdt = float(parts[2]) if len(parts) > 2 else load_config().get("max_usdt_per_trade", 150)

        if not sym:
            send_telegram_message("❌ Please specify a coin or number.\nExample: /buy ARIA 200 or /buy 1 200")
            return True

        price = get_prices(sym)[0]
        if price > 0:
            amount = usdt / price
            record_trade({"type": "BUY", "symbol": sym, "price": price, "amount": amount, "usdt_amount": usdt, "timestamp": datetime.now().isoformat()})
            update_position(sym, "4h", "BUY", price, amount)
            send_telegram_message(f"✅ Virtual BUY executed: {sym} ${usdt:.0f} @ ${price:.4f}")
        else:
            send_telegram_message(f"❌ Could not fetch price for {sym}. Check if the coin exists.")
        return True

    elif text.startswith("/sell"):
        parts = text.split()
        if len(parts) == 1:
            active = list_active_positions()
            if not active:
                send_telegram_message("❌ No active positions to sell.")
                return True
            msg = "<b>📍 Active Positions to Sell:</b>\n\n"
            for i, p in enumerate(active, 1):
                highlight = p.get("highlight", "")
                price = get_prices(p["symbol"] + "/USDT" if "/" not in p["symbol"] else p["symbol"])[0]
                entry = p.get("average_entry", p.get("entry_price", 0))
                unreal = (price - entry) * p["amount"] if entry > 0 and price > 0 else 0
                msg += f"{i}. {highlight}{p['symbol']} | Amt: {p['amount']:.4f} | Entry: ${entry:.4f} | Unreal: ${unreal:.1f}\n"
            msg += "\nUse <code>/sell NUMBER PERCENT</code> (e.g. /sell 1 30)"
            send_telegram_message(msg)
            return True
        else:
            try:
                idx = int(parts[1]) - 1
                pct = float(parts[2]) / 100 if len(parts) > 2 else 0.5
                active = list_active_positions()
                if 0 <= idx < len(active):
                    p = active[idx]
                    sym = p["symbol"] + "/USDT" if "/" not in p["symbol"] else p["symbol"]
                    price = get_prices(sym)[0]
                    if price > 0:
                        pos = get_position(sym, "4h")
                        amount_sold = float(pos.get("amount", 0) * pct)
                        if amount_sold > 0:
                            received = price * amount_sold
                            pnl = (price - pos.get("entry_price", price)) * amount_sold
                            record_trade({"type": "SELL", "symbol": sym, "price": price, "amount": amount_sold, "usdt_received": received, "pnl": pnl, "timestamp": datetime.now().isoformat()})
                            update_position(sym, "4h", "SELL", price, amount_sold)
                            send_telegram_message(f"✅ Sold {pct*100:.0f}% of {sym} @ ${price:.4f} (PnL: ${pnl:.1f})")
                            return True
                send_telegram_message("❌ Invalid selection.")
            except:
                send_telegram_message("❌ Usage: /sell NUMBER PERCENT")
            return True

    elif text in ["/positions", "/status", "/balance"]:
        active = list_active_positions()
        history = load_trade_history()
        total_unreal = 0.0
        for p in active:
            price = get_prices(p["symbol"] + "/USDT" if "/" not in p["symbol"] else p["symbol"])[0]
            if price > 0 and p.get("entry_price", 0) > 0:
                total_unreal += (price - p["entry_price"]) * p["amount"]
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
        for p in active:
            highlight = p.get("highlight", "")
            price = get_prices(p["symbol"] + "/USDT" if "/" not in p["symbol"] else p["symbol"])[0]
            entry = p.get("average_entry", p.get("entry_price", 0))
            unreal = (price - entry) * p["amount"] if entry > 0 and price > 0 else 0
            unreal_pct = (unreal / (entry * p["amount"]) * 100) if entry > 0 else 0
            msg += f"{highlight}{p['symbol']:8} | Amt: {p['amount']:.4f} | Entry: ${entry:.4f} | Unreal: ${unreal:.1f} ({unreal_pct:+.1f}%)\n"

        msg += "\n<b>── Last Trades ──</b>\n"
        trades = history.get("trades", [])[-8:]
        for t in reversed(trades):
            ts = t.get("timestamp", "")[:16].replace("T", " ")
            typ = "🟢 BUY" if t.get("type") == "BUY" else "🔴 SELL"
            pnl_str = f" PnL:${t.get('pnl', 0):+.1f}" if t.get("pnl") is not None else ""
            msg += f"{ts} | {typ} | {t.get('symbol',''):<10} | ${t.get('price',0):.4f} | Amt:{t.get('amount',0):.4f}{pnl_str}\n"

        send_telegram_message(msg)
        return True

    elif text.startswith("/addx "):
        handle = text[6:].strip().replace("@", "")
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
        handle = text[8:].strip().replace("@", "")
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

    elif text in ["/xsignals", "/signals", "/xs"]:
        from x_analyzer import XAnalyzer
        analyzer = XAnalyzer()
        signals = analyzer.fetch_latest_signals()
        if not signals:
            send_telegram_message("No high-confidence X signals found.")
        else:
            msg = "<b>📡 Latest X Signals:</b>\n\n"
            for s in signals:
                emoji = "🟢" if s.action == "BUY" else "🔴" if s.action == "SELL" else "⏸️"
                target = f" Target ~${s.price_target}" if s.price_target else ""
                msg += f"{emoji} <b>{s.action}</b> {s.coin} | Confidence: {s.confidence}% | From: @{s.account}{target}\n"
                if s.rationale:
                    msg += f"   └ {s.rationale[:80]}...\n\n"
            send_telegram_message(msg)
        return True
    elif text in ["/help", "/commands", "/?" ]:
        msg = """<b>🛠️ Available Commands:</b>

<b>Watchlist:</b>
/add SYMBOL - Add coin (e.g. /add RAVE)
/remove NUMBER - Remove by number (first /list)
/list or /watchlist - Show all coins

<b>Trading:</b>
/buy SYMBOL [USDT] - Virtual buy (e.g. /buy ARIA 200)
/sell NUMBER PERCENT - Sell from position (first /sell to list)
/positions or /status - Portfolio overview with PnL and trades

<b>X Accounts:</b>
/addx ACCOUNT - Add X account (e.g. /addx CryptoCapo_)
/removex ACCOUNT - Remove X account
/listx - List monitored X accounts
/xsignals - Show latest parsed X signals

Send /help anytime for this list.
"""
        send_telegram_message(msg)
        return True

    return False



