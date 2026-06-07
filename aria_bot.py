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
    from services.signal_orchestrator import SignalOrchestrator
    from services.social_pipeline import SocialPipeline
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


def price_loop(analyzer=None, orchestrator=None, social_pipeline=None):
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

            if social_pipeline:
                social_pipeline.process_new_posts()
                accuracy = social_pipeline.update_accuracy_loop()
                if accuracy["outcomes_updated"] or accuracy["trust_updates"]:
                    print(f"   Accuracy update: {accuracy['outcomes_updated']} outcomes, {accuracy['trust_updates']} trust scores")

            x_signals = social_pipeline.refresh_signals() if social_pipeline else (analyzer.get_top_signals() if analyzer else [])

            for signal in x_signals:
                eff = getattr(signal, "effective_confidence", signal.confidence)
                if eff >= 70:
                    print(
                        f"   → X-Signal @{signal.account}: {signal.action} {signal.coin} | "
                        f"Conf: {signal.confidence}% | Effective: {eff:.0f}% | Trust: {getattr(signal, 'trust_score', '?')}"
                    )

            for coin in watchlist:
                if not coin.get("active", True):
                    continue
                symbol = coin["symbol"]
                print(f"→ {symbol}")

                dex_price, cg_price, diff = get_prices(symbol)
                price = dex_price if dex_price is not None else 0.0
                if orchestrator:
                    orchestrator.process_coin(coin, price, x_signals)
                else:
                    from strategies.core_strategy import check_signal
                    check_signal(coin, price, x_signals, notify_callback=send_signal_message)
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
    orchestrator = SignalOrchestrator(notify_callback=send_signal_message)
    social_pipeline = SocialPipeline(analyzer, orchestrator=orchestrator)
    price_thread = threading.Thread(
        target=price_loop, args=(analyzer, orchestrator, social_pipeline), daemon=True
    )
    price_thread.start()

    print(get_text("webhook_started"))

    app.run(port=5000)

