#!/usr/bin/env python3
"""Fast smoke test for sell fixes + Telegram output (demo mode)."""
import os
import sys
import time
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ["DEMO_MODE"] = "1"

from dotenv import load_dotenv

load_dotenv()

from data_manager import load_watchlist
from notifications.telegram_commands.router import dispatch_command
from price_fetcher import get_prices
from services.signal_orchestrator import SignalOrchestrator
from strategies.positions import get_position
from telegram_notifier import (
    send_signal_message,
    send_telegram_message,
    send_x_recommendation_message,
)


def section(title: str):
    send_telegram_message(f"<b>🧪 Schnelltest — {title}</b>")
    time.sleep(0.5)


def main() -> int:
    send_telegram_message(
        "<b>🚀 Schnelltest gestartet</b>\n"
        "Branch: feature/fix-phantom-sell-signals\n"
        "Modus: Demo + Paper"
    )
    time.sleep(0.6)

    # 1) Phantom sell — no position, high RSI
    section("1/5 Phantom-Sell (kein Bestand)")
    notifications = []
    orch = SignalOrchestrator(
        notify_callback=lambda *args, **kwargs: notifications.append((args, kwargs))
    )
    phantom = {"symbol": "PHANTOM/USDT", "timeframe": "4h", "name": "Phantom", "active": True}
    with patch.object(
        orch.market,
        "fetch_indicators",
        return_value={"rsi": 75.0, "lower_bb": 0.60, "vol_multiplier": 1.0},
    ):
        result = orch.process_coin(phantom, 0.65)
    ok = result["action"] == "HOLD" and not result["executed"] and len(notifications) == 0
    send_telegram_message(
        f"{'✅' if ok else '❌'} PHANTOM/USDT RSI 75 ohne Position\n"
        f"Action: <code>{result['action']}</code> | Executed: {result['executed']}\n"
        f"Telegram-Alerts: {len(notifications)} (erwartet: 0)"
    )
    time.sleep(0.6)

    # 2) Telegram title variants
    section("2/5 Telegram-Titel (SIGNAL / EXECUTED / BLOCKED)")
    coin = {"symbol": "RAVE/USDT", "name": "RaveDAO"}
    send_signal_message("SELL_30", coin, 0.65, 75.0, 0.60, 0.8, "🔴", "Bearish", executed=None)
    time.sleep(0.4)
    send_signal_message("SELL_20", coin, 0.65, 72.0, 0.60, 0.8, "🔴", "Bearish", executed=True)
    time.sleep(0.4)
    send_signal_message(
        "SELL_30",
        coin,
        0.65,
        75.0,
        0.60,
        0.8,
        "🔴",
        "Bearish",
        executed=False,
        trade_message="Kein Bestand zum Verkaufen",
    )
    time.sleep(0.6)

    # 3) X recommendation with TP/SL display gap noted
    section("3/5 X-Recommendation")
    send_x_recommendation_message(
        {
            "account": "CryptoCapo_",
            "coin": "BTC",
            "action": "SELL",
            "confidence": 78,
            "rationale": "Schnelltest — SELL-Empfehlung aus X-Pipeline",
            "raw_tweet": "BTC taking profits here, target reached...",
            "price_target": 105000.0,
            "stop_loss": 98000.0,
        }
    )
    time.sleep(0.6)

    # 4) One real-price watchlist cycle (active coins only, capped)
    section("4/5 Watchlist-Zyklus (echte Preise)")
    watchlist = [c for c in load_watchlist() if c.get("active", True)][:4]
    cycle_results = []
    orch_live = SignalOrchestrator(notify_callback=send_signal_message)
    for coin in watchlist:
        symbol = coin["symbol"]
        if "/" not in symbol:
            symbol = f"{symbol}/USDT"
            coin = {**coin, "symbol": symbol}
        price, _, _ = get_prices(symbol)
        if not price:
            continue
        r = orch_live.process_coin(coin, price, quiet=True)
        cycle_results.append(r)
        time.sleep(0.3)

    lines = []
    for r in cycle_results:
        pos = get_position(r["symbol"], "4h")
        amt = float(pos.get("amount", 0))
        lines.append(
            f"{r['symbol']}: {r['action']} | RSI {r.get('rsi', 0):.0f} | "
            f"Pos {amt:.2f} | Exec {r.get('executed', False)}"
        )
    send_telegram_message(
        "<b>Watchlist-Ergebnis:</b>\n" + ("\n".join(lines) if lines else "Keine Preise geladen")
    )
    time.sleep(0.6)

    # 5) Key Telegram commands
    section("5/5 Bot-Commands")
    for cmd in ("/mode", "/positions", "/sell"):
        dispatch_command(cmd)
        time.sleep(0.5)

    send_telegram_message("<b>✅ Schnelltest abgeschlossen</b>")
    print("Quick smoke test finished — check Telegram for messages.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())