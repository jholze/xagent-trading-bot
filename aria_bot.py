import os
import threading
import time
import json
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, request

import argparse

from logger import log

load_dotenv()

# Demo mode handling
parser = argparse.ArgumentParser(description="X-Agent Trading Bot")
parser.add_argument('--demo', action='store_true', help='Run in demo mode using separate .demo.json data files')
args, _ = parser.parse_known_args()

if args.demo:
    os.environ['DEMO_MODE'] = '1'
    print("🧪 Demo mode activated - using separate data files (watchlist.demo.json, etc.)")

try:
    from data_manager import get_text
except:
    def get_text(key, default=""):
        return default or key

print(get_text("bot_started") + "\n")

try:
    from data_manager import (
        get_config,
        get_text,
        list_coins,
        load_trade_history,
        load_watchlist,
    )
    from price_fetcher import get_prices
    from strategies.core_strategy import check_signal
    from telegram_notifier import handle_telegram_command, send_signal_message
    from x_analyzer import XAnalyzer
except ImportError as e:
    print(f"Fehler beim Laden der Module: {e}")
    exit()

with open("config.json", encoding="utf-8") as f:
    config = json.load(f)
virtual_trading = config.get("virtual_trading", True)
print(get_text("virtual_trading_enabled" if virtual_trading else "virtual_trading_disabled"))

# Flask für Webhook
app = Flask(__name__)


@app.route("/", methods=["POST"])
def webhook():
    try:
        update = request.get_json()
        if update and "message" in update:
            text = update["message"].get("text", "")
            log(f"Received Telegram message: {text[:100]}", "DEBUG")
            if text.startswith("/"):
                handle_telegram_command(text)
    except Exception as e:
        log(f"Webhook error: {e}", "ERROR")
    return "OK", 200


def price_loop(analyzer=None):
    while True:
        try:
            # Safely clear screen only in interactive terminals
            if os.isatty(1):
                os.system("clear" if os.name == "posix" else "cls")
            now = datetime.now()
            print(f"🕒 {now.strftime('%H:%M:%S')}                  X-Agent Trading Bot                  Virtual Trading: {'ON' if virtual_trading else 'OFF'}")
            print("=" * 90)

            watchlist = load_watchlist()
            active_symbols = [
                coin["symbol"] for coin in watchlist if coin.get("active", True)
            ]
            print(f"Aktive Coins ({len(active_symbols)}): " + " • ".join(active_symbols))
            print("-" * 90)

            print("Prüfe Coins + X-Signale:\n")

            x_signals = analyzer.get_top_signals() if analyzer else []

            for signal in x_signals:
                if signal.confidence >= 75:
                    print(f"   → Strong X-Signal from @{signal.account}: {signal.action} {signal.coin} | Confidence: {signal.confidence}%")

            for coin in watchlist:
                if not coin.get("active", True):
                    continue
                symbol = coin["symbol"]
                print(f"→ {symbol}")

                dex_price, cg_price, diff = get_prices(symbol)
                check_signal(coin, dex_price if dex_price is not None else 0.0, x_signals)
                print()

            print("-" * 90)
            print(f"Update abgeschlossen um {now.strftime('%H:%M:%S')}")

            interval = get_config().get("update_interval", 600)
            for remaining in range(interval, 0, -1):
                print(f"\r   Nächste Aktualisierung in {remaining:3d} Sekunden...", end="", flush=True)
                time.sleep(1)
            print("\n")

        except Exception as e:
            log(f"Error in price loop: {e}", "ERROR")
            time.sleep(get_config().get("update_interval", 600))


if __name__ == "__main__":
    analyzer = XAnalyzer()

    price_thread = threading.Thread(target=price_loop, args=(analyzer,), daemon=True)
    price_thread.start()

    print(get_text("webhook_started"))

    app.run(port=5000)

