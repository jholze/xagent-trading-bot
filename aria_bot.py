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
    from core.config import get_bot_config
    from data_manager import (
        get_config,
        get_text,
        list_coins,
        load_trade_history,
        load_watchlist,
    )
    from notifications.terminal_dashboard import build_cycle_summary, render_cycle_dashboard
    from price_fetcher import get_prices
    from intelligence.trend_engine import TrendEngine
    from services.signal_orchestrator import SignalOrchestrator
    from services.social_pipeline import SocialPipeline
    from strategies.paper_sandbox import PaperSandbox
    from telegram_notifier import handle_telegram_callback, handle_telegram_command, send_cycle_summary, send_signal_message
    from x_analyzer import XAnalyzer
except ImportError as e:
    print(f"Fehler beim Laden der Module: {e}")
    exit()

with open("config.json", encoding="utf-8") as f:
    config = json.load(f)
trading_mode = config.get("trading_mode", "paper" if config.get("virtual_trading", True) else "off")
print(f"Trading mode: {trading_mode.upper()}" + (" (demo)" if os.environ.get("DEMO_MODE") == "1" else ""))

try:
    from services.ledger_sync import sync_positions_on_startup
    sync_positions_on_startup()
except Exception as e:
    log(f"Ledger position sync on startup failed: {e}", "WARNING")

# Flask für Webhook
app = Flask(__name__)


@app.route("/health", methods=["GET"])
def health():
    return "OK", 200


@app.route("/", methods=["POST"])
def webhook():
    try:
        update = request.get_json()
        if update and "callback_query" in update:
            log("Received Telegram callback query", "DEBUG")
            handle_telegram_callback(update["callback_query"])
        elif update and "message" in update:
            text = update["message"].get("text", "")
            log(f"Received Telegram message: {text[:100]}", "DEBUG")
            if text.startswith("/"):
                handle_telegram_command(text)
    except Exception as e:
        log(f"Webhook error: {e}", "ERROR")
    return "OK", 200


def price_loop(analyzer=None, orchestrator=None, social_pipeline=None, sandbox=None, trend_engine=None):
    bot_config = get_bot_config()
    use_dashboard = bot_config.terminal_dashboard_enabled and os.isatty(1)

    while True:
        try:
            bot_config.refresh()
            use_dashboard = bot_config.terminal_dashboard_enabled and os.isatty(1)

            if not use_dashboard and os.isatty(1):
                os.system("clear" if os.name == "posix" else "cls")

            now = datetime.now()
            mode = get_config().get("trading_mode", "paper")
            cycle_signal_lines = []
            coin_results = []

            if not use_dashboard:
                print(f"🕒 {now.strftime('%H:%M:%S')}                  X-Agent Trading Bot                  Mode: {mode.upper()}")
                print("=" * 90)

            watchlist = load_watchlist()
            active_symbols = [
                coin["symbol"] for coin in watchlist if coin.get("active", True)
            ]
            if not use_dashboard:
                print(f"Aktive Coins ({len(active_symbols)}): " + " • ".join(active_symbols))
                print("-" * 90)
                print("Prüfe Coins + X-Signale:\n")

            if social_pipeline:
                social_pipeline.process_new_posts()
                social_pipeline.process_cmc_posts(watchlist)
                accuracy = social_pipeline.update_accuracy_loop()
                if not use_dashboard and (accuracy["outcomes_updated"] or accuracy["trust_updates"]):
                    print(f"   Accuracy update: {accuracy['outcomes_updated']} outcomes, {accuracy['trust_updates']} trust scores")

            x_signals = social_pipeline.refresh_signals() if social_pipeline else (analyzer.get_top_signals() if analyzer else [])
            cmc_signals = social_pipeline.refresh_cmc_signals() if social_pipeline else []

            if trend_engine and x_signals:
                candidates = trend_engine.cross_validate(x_signals, run_scan=False)
                for c in candidates[:3]:
                    line = f"→ Trend+X: {c['symbol']} ({c['regime']}) 5m:{c['change_5m']:+.1f}%"
                    cycle_signal_lines.append(line)
                    if not use_dashboard:
                        print(f"   {line}")

            if sandbox and get_config().get("sandbox", {}).get("enabled", True):
                sandbox_results = sandbox.run_cycle(watchlist, get_prices)
                for sr in sandbox_results[:3]:
                    m = sr["metrics"]
                    line = f"→ Sandbox {sr['hypothesis_id']}: {sr['action']} {sr['symbol']} | WR={m.win_rate}%"
                    cycle_signal_lines.append(line)
                    if not use_dashboard:
                        print(f"   {line}")

            for signal in x_signals:
                eff = getattr(signal, "effective_confidence", signal.confidence)
                if eff >= 70:
                    line = (
                        f"🟢 @{signal.account} {signal.action} {signal.coin} | "
                        f"Conf: {signal.confidence}% | Eff: {eff:.0f}%"
                    )
                    cycle_signal_lines.append(line)
                    if not use_dashboard:
                        print(f"   → X-Signal @{signal.account}: {signal.action} {signal.coin} | "
                              f"Conf: {signal.confidence}% | Effective: {eff:.0f}% | "
                              f"Trust: {getattr(signal, 'trust_score', '?')}")

            for signal in cmc_signals:
                if signal.confidence >= 60:
                    line = f"📊 CMC {signal.action} {signal.coin} | {signal.confidence}%"
                    cycle_signal_lines.append(line)
                    if not use_dashboard:
                        print(
                            f"   → CMC Community: {signal.action} {signal.coin} | "
                            f"Conf: {signal.confidence}% | Votes: {signal.votes_bullish}↑/{signal.votes_bearish}↓"
                        )

            for coin in watchlist:
                if not coin.get("active", True):
                    continue
                symbol = coin["symbol"]
                if not use_dashboard:
                    print(f"→ {symbol}")

                dex_price, cg_price, diff = get_prices(symbol)
                price = dex_price if dex_price is not None else 0.0
                if orchestrator:
                    result = orchestrator.process_coin(
                        coin, price, x_signals, cmc_signals, quiet=use_dashboard
                    )
                    coin_results.append(result)
                else:
                    from strategies.core_strategy import check_signal
                    check_signal(coin, price, x_signals, notify_callback=send_signal_message)
                if not use_dashboard:
                    print()

            interval = get_config().get("update_interval", 600)

            if use_dashboard:
                render_cycle_dashboard(
                    cycle_signals=cycle_signal_lines,
                    coin_results=coin_results,
                    trading_mode=mode,
                    next_update=interval,
                )
            else:
                print("-" * 90)
                print(f"Update abgeschlossen um {now.strftime('%H:%M:%S')}")

            summary = build_cycle_summary(
                coin_results=coin_results,
                trading_mode=mode,
                x_signal_count=len(x_signals),
                cmc_signal_count=len(cmc_signals),
            )
            send_cycle_summary(summary)

            for remaining in range(interval, 0, -1):
                if not use_dashboard:
                    print(f"\r   Nächste Aktualisierung in {remaining:3d} Sekunden...", end="", flush=True)
                time.sleep(1)
            if not use_dashboard:
                print("\n")

        except Exception as e:
            log(f"Error in price loop: {e}", "ERROR")
            time.sleep(get_config().get("update_interval", 600))


if __name__ == "__main__":
    analyzer = XAnalyzer()
    orchestrator = SignalOrchestrator(notify_callback=send_signal_message)
    social_pipeline = SocialPipeline(analyzer, orchestrator=orchestrator)
    sandbox = PaperSandbox()
    trend_engine = TrendEngine()
    price_thread = threading.Thread(
        target=price_loop,
        args=(analyzer, orchestrator, social_pipeline, sandbox, trend_engine),
        daemon=True,
    )
    price_thread.start()

    print(get_text("webhook_started"))

    app.run(port=5000)