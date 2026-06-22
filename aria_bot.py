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
        load_effective_watchlist,
    )
    from notifications.terminal_dashboard import build_cycle_summary, render_cycle_dashboard
    from price_fetcher import get_prices, get_prices_batch
    from intelligence.trend_engine import TrendEngine
    from services.signal_orchestrator import SignalOrchestrator
    from services.social_pipeline import SocialPipeline
    from strategies.paper_sandbox import PaperSandbox
    from telegram_notifier import (
        handle_telegram_callback,
        handle_telegram_command,
        handle_telegram_text,
        send_cmc_cycle_digest,
        send_cycle_summary,
        send_lc_cycle_digest,
        send_signal_message,
        send_x_cycle_digest,
    )
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
        if update:
            from notifications.telegram_commands.menu_i18n import set_user_language_from_update

            set_user_language_from_update(update)
        if update and "callback_query" in update:
            log("Received Telegram callback query", "DEBUG")
            handle_telegram_callback(update["callback_query"])
        elif update and "message" in update:
            text = update["message"].get("text", "")
            chat_id = update["message"].get("chat", {}).get("id")
            log(f"Received Telegram message: {text[:100]}", "DEBUG")
            if text.startswith("/"):
                handle_telegram_command(text, chat_id=chat_id)
            elif text.strip():
                handle_telegram_text(text, chat_id=chat_id)
    except Exception as e:
        log(f"Webhook error: {e}", "ERROR")
    return "OK", 200


def price_loop(analyzer=None, orchestrator=None, social_pipeline=None, sandbox=None, trend_engine=None):
    bot_config = get_bot_config()
    use_dashboard = bot_config.terminal_dashboard_enabled and os.isatty(1)

    while True:
        try:
            cycle_started = time.time()
            bot_config.refresh()
            try:
                from services.architecture_runtime import ensure_started
                ensure_started()
            except Exception as e:
                log(f"Architecture runtime tick failed: {e}", "WARNING")
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

            try:
                from services.dry_run_watchlist import DryRunWatchlistSync
                DryRunWatchlistSync(bot_config).sync_if_needed()
            except Exception as e:
                log(f"Dry-run watchlist sync failed: {e}", "WARNING")

            watchlist = load_effective_watchlist()
            active_symbols = [
                coin["symbol"] for coin in watchlist if coin.get("active", True)
            ]
            if not use_dashboard:
                print(f"Aktive Coins ({len(active_symbols)}): " + " • ".join(active_symbols))
                print("-" * 90)
                print("Prüfe Coins + X-Signale:\n")

            if social_pipeline:
                accuracy = social_pipeline.run_cycle_fetches(watchlist)
                if not use_dashboard and (accuracy["outcomes_updated"] or accuracy["trust_updates"]):
                    print(f"   Accuracy update: {accuracy['outcomes_updated']} outcomes, {accuracy['trust_updates']} trust scores")

            x_signals = social_pipeline.refresh_signals() if social_pipeline else (analyzer.get_top_signals() if analyzer else [])
            cmc_signals = social_pipeline.refresh_cmc_signals() if social_pipeline else []
            lc_signals = social_pipeline.refresh_lc_signals() if social_pipeline else []

            if bot_config.architecture_config.get("use_signal_snapshot"):
                try:
                    from bus.publisher import publish_signal_snapshot
                    from bus.signals import signal_snapshot_store

                    snap = signal_snapshot_store.publish(
                        x_signals=[getattr(s, "__dict__", s) for s in (x_signals or [])[:50]],
                        cmc_signals=[getattr(s, "__dict__", s) for s in (cmc_signals or [])[:50]],
                        lc_signals=[getattr(s, "__dict__", s) for s in (lc_signals or [])[:50]],
                        watchlist_symbols=active_symbols,
                    )
                    arch = bot_config.architecture_config
                    publish_signal_snapshot(snap, key_prefix=arch.get("key_prefix", "aria:"), redis_url=arch.get("redis_url"))
                except Exception as e:
                    log(f"Signal snapshot publish failed: {e}", "WARNING")

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

            for signal in lc_signals:
                if signal.confidence >= 55:
                    line = (
                        f"🌙 LC {signal.action} {signal.coin} | {signal.confidence}% | "
                        f"Galaxy {signal.galaxy_score:.0f}"
                    )
                    cycle_signal_lines.append(line)
                    if not use_dashboard:
                        print(
                            f"   → LunarCrush: {signal.action} {signal.coin} | "
                            f"Conf: {signal.confidence}% | Galaxy: {signal.galaxy_score:.0f} | "
                            f"AltRank: {signal.alt_rank} | Sentiment: {signal.sentiment:.0f}%"
                        )

            active_coins = [coin for coin in watchlist if coin.get("active", True)]
            price_map = get_prices_batch([coin["symbol"] for coin in active_coins])

            for coin in active_coins:
                symbol = coin["symbol"]
                if not use_dashboard:
                    print(f"→ {symbol}")

                price = float(price_map.get(symbol, 0) or 0)
                if orchestrator:
                    result = orchestrator.process_coin(
                        coin, price, x_signals, cmc_signals, lc_signals, quiet=use_dashboard
                    )
                    coin_results.append(result)
                else:
                    from strategies.core_strategy import check_signal
                    check_signal(coin, price, x_signals, notify_callback=send_signal_message)
                if not use_dashboard:
                    print()

            interval = get_config().get("update_interval", 600)
            cycle_elapsed = int(time.time() - cycle_started)
            if cycle_elapsed > 30:
                log(f"Cycle completed in {cycle_elapsed}s ({len(active_coins)} coins)", "INFO")

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

            top_x = ""
            top_cmc = ""
            top_lc = ""
            if x_signals:
                best_x = max(
                    x_signals,
                    key=lambda s: getattr(s, "effective_confidence", getattr(s, "confidence", 0)),
                )
                eff = getattr(best_x, "effective_confidence", best_x.confidence)
                top_x = f"@{best_x.account} {best_x.action} {best_x.coin} ({eff:.0f}%)"
            if cmc_signals:
                best_cmc = max(cmc_signals, key=lambda s: s.confidence)
                top_cmc = (
                    f"{best_cmc.coin} {best_cmc.action} ({best_cmc.confidence}%) "
                    f"Votes {best_cmc.votes_bullish}↑/{best_cmc.votes_bearish}↓"
                )

            if lc_signals:
                best_lc = max(lc_signals, key=lambda s: s.confidence)
                top_lc = (
                    f"{best_lc.coin} {best_lc.action} ({best_lc.confidence}%) "
                    f"Galaxy {best_lc.galaxy_score:.0f}"
                )

            if social_pipeline:
                if social_pipeline.should_send_cmc_digest(cmc_signals):
                    send_cmc_cycle_digest(cmc_signals)
                if social_pipeline.should_send_lc_digest(lc_signals):
                    send_lc_cycle_digest(lc_signals)
                send_x_cycle_digest(x_signals, skip_post_ids=social_pipeline.get_notified_post_ids())

            summary = build_cycle_summary(
                coin_results=coin_results,
                trading_mode=mode,
                x_signal_count=len(x_signals),
                cmc_signal_count=len(cmc_signals),
                lc_signal_count=len(lc_signals),
                top_x=top_x,
                top_cmc=top_cmc,
                top_lc=top_lc,
            )
            send_cycle_summary(summary)

            try:
                from services.strategy_backtest_worker import tick_strategy_backtest
                tick_strategy_backtest()
            except Exception as e:
                log(f"Strategy backtest tick failed: {e}", "WARNING")

            sleep_seconds = max(0, interval - cycle_elapsed)
            if sleep_seconds == 0 and cycle_elapsed >= interval:
                log(f"Cycle took {cycle_elapsed}s (>= interval {interval}s) — starting next immediately", "WARNING")

            for remaining in range(sleep_seconds, 0, -1):
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
    try:
        from notifications.coin_links import prefetch_watchlist_slugs

        prefetch_watchlist_slugs()
    except Exception as e:
        log(f"Coin link slug prefetch skipped: {e}", "WARNING")

    try:
        from notifications.telegram_commands.command_menu import register_bot_commands

        register_bot_commands()
    except Exception as e:
        log(f"Telegram command menu registration skipped: {e}", "WARNING")

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

    try:
        from services.webhook_watchdog import start_webhook_watchdog

        start_webhook_watchdog()
    except Exception as e:
        log(f"Webhook watchdog not started: {e}", "WARNING")

    try:
        from services.telegram_ask_bridge import start_ask_bridge_poller

        start_ask_bridge_poller()
    except Exception as e:
        log(f"Ask bridge poller not started: {e}", "WARNING")

    try:
        from services.architecture_runtime import ensure_started
        ensure_started()
    except Exception as e:
        log(f"Architecture runtime start failed: {e}", "WARNING")

    bot_config = get_bot_config()
    from services.architecture_runtime import hermes_runs_in_process
    if hermes_runs_in_process(bot_config):
        from hermes.agent import HermesAgent

        hermes_interval = int(bot_config.hermes_config.get("cycle_interval_sec", 3600))

        def hermes_loop():
            agent = HermesAgent(bot_config)
            while True:
                try:
                    bot_config.refresh()
                    result = agent.run_cycle()
                    log(result.summary, "INFO")
                except Exception as e:
                    log(f"Hermes loop error: {e}", "ERROR")
                time.sleep(hermes_interval)

        threading.Thread(target=hermes_loop, daemon=True, name="hermes-agent").start()
        print(f"Hermes self-improvement loop started (interval={hermes_interval}s)")

    print(get_text("webhook_started"))

    app.run(port=5000, threaded=True)