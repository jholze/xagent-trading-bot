import os
import threading
import time
import json
import itertools
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, request

load_dotenv()

try:
    from data_manager import get_text
except:
    def get_text(key, default=""):
        return default or key

print(get_text("bot_started") + "\n")

try:
    from data_manager import (  # <-- nur die benötigten Funktionen
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
dry_run = config.get("dry_run", True)
print(get_text("dry_run_enabled" if dry_run else "dry_run_disabled"))

# Flask für Webhook
app = Flask(__name__)


@app.route("/", methods=["POST"])
def webhook():
    update = request.get_json()
    if update and "message" in update:
        text = update["message"].get("text", "")
        print(f"[DEBUG] Empfangene Nachricht: {text}")
        if text.startswith("/"):
            handle_telegram_command(text)
    return "OK", 200


def price_loop(analyzer=None):
    while True:
        try:
            os.system("clear" if os.name == "posix" else "cls")
            now = datetime.now()
            print(f"🕒 {now.strftime('%H:%M:%S')}                  X-Agent Trading Bot                  Dry-Run: {'ON' if dry_run else 'OFF'}")
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
                check_signal(coin, dex_price if dex_price is not None else 0.0, dry_run, x_signals)
                print()

            print("-" * 90)
            print(f"Update abgeschlossen um {now.strftime('%H:%M:%S')}")

            for remaining in range(60, 0, -1):
                print(f"\r   Nächste Aktualisierung in {remaining:2d} Sekunden...", end="", flush=True)
                time.sleep(1)
            print("\n")

        except Exception as e:
            print(f"Fehler im Price-Loop: {e}")
            time.sleep(60)


        except Exception as e:
            print(f"Fehler im Price-Loop: {e}")
            time.sleep(60)



        except Exception as e:
            print(f"Fehler im Price-Loop: {e}")
            time.sleep(60)


        except Exception as e:
            print(f"Fehler im Price-Loop: {e}")
            time.sleep(60)


        except Exception as e:
            print(f"Fehler im Price-Loop: {e}")
            time.sleep(60)


        except Exception as e:
            print(f"Fehler im Price-Loop: {e}")
            time.sleep(60)



if __name__ == "__main__":
    analyzer = XAnalyzer()

    price_thread = threading.Thread(target=price_loop, args=(analyzer,), daemon=True)
    price_thread.start()

    print(get_text("webhook_started"))

    app.run(port=5000)

