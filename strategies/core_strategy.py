import time
from datetime import datetime
from decimal import Decimal

import ccxt
import pandas as pd
import talib
from logger import log, log_signal
from telegram_notifier import send_signal_message
from strategies.positions import get_position, update_position
from data_manager import get_config, get_text, load_trade_history, record_trade


def get_ampel_color(rsi, vol_multiplier, price, lower_bb):
    if rsi is None or vol_multiplier is None:
        return "🟡", "Neutral"

    if vol_multiplier >= 1.7 and rsi <= 48:
        return "🟢", "Stark Bullish"
    elif rsi <= 42 and price <= lower_bb * 1.015:
        return "🟢", "Bullish (Tief)"
    elif rsi >= 68:
        return "🔴", "Bearish"
    elif vol_multiplier <= 0.6:
        return "🔴", "Schwaches Volumen"
    else:
        return "🟡", "Neutral"


def check_signal(coin, current_price, x_signals=None):
    if not current_price:
        return "HOLD"

    symbol = coin["symbol"]
    tf = coin.get("timeframe", "4h")

    pos = get_position(symbol, tf)
    has_position = pos["amount"] > 0

    # Find relevant X signal for this coin
    x_signal = next((s for s in (x_signals or []) if s.coin == symbol.split("/")[0]), None)
    x_score = x_signal.score if x_signal else 0.0
    x_confidence = x_signal.confidence if x_signal else 0

    last_rsi = 45.0
    last_lower = current_price * 0.97
    vol_multiplier = 1.2
    df = None

    exchanges = ["gate", "binance", "kucoin", "bybit"]
    for ex_name in exchanges:
        try:
            exchange = getattr(ccxt, ex_name)({"enableRateLimit": True, "timeout": 12000})
            bars = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=100)
            df = pd.DataFrame(bars, columns=["ts", "open", "high", "low", "close", "volume"])
            df["rsi"] = talib.RSI(df["close"], timeperiod=14)
            _, _, df["lower"] = talib.BBANDS(df["close"], timeperiod=20)
            df["vol_avg"] = df["volume"].rolling(window=20).mean()
            recent_vol_avg = df["volume"].tail(4).mean()
            long_vol_avg = df["vol_avg"].iloc[-1]
            vol_multiplier = recent_vol_avg / long_vol_avg if long_vol_avg and long_vol_avg > 0 else 1.0
            last_rsi = df["rsi"].iloc[-1]
            last_lower = df["lower"].iloc[-1]
            break
        except Exception as e:
            log(f"{ex_name.capitalize()} fetch failed for {symbol}: {e}", "WARNING")
            continue

    if df is None:
        log(f"All exchanges failed for {symbol}. Using fallback data.", "ERROR")
        last_rsi = 45.0
        last_lower = current_price * 0.97
        vol_multiplier = 1.3
        df = pd.DataFrame()

    config = get_config()
    history = load_trade_history()
    ampel_emoji, ampel_text = get_ampel_color(last_rsi, vol_multiplier, current_price, last_lower)

    signal = "HOLD"
    x_boost = x_score > 0.6

    if not has_position and history.get("open_positions", 0) < config.get("max_open_positions", 5):
        buy_threshold = 48 if x_boost else 45
        if (current_price <= last_lower * 1.01 and 28 <= last_rsi <= buy_threshold and vol_multiplier >= 1.2) or (x_signal and x_signal.action == "BUY" and x_confidence >= 75):
            signal = "BUY"
    else:
        pos = get_position(symbol, tf)
        entry = pos.get("entry_price", current_price)
        if entry > 0:
            loss_pct = (current_price / entry - 1) * -100
            if loss_pct > 15 or (x_signal and x_signal.action == "SELL" and x_confidence >= 80):
                signal = "SELL_STOP_FULL"
            elif loss_pct > 8:
                signal = "SELL_STOP_PARTIAL"
        if last_rsi >= 80 or (x_signal and x_signal.action == "SELL" and x_confidence >= 70):
            signal = "SELL_20"
        elif last_rsi >= 70:
            signal = "SELL_30"

    if signal != "HOLD" and config.get("virtual_trading", True):
        pos = get_position(symbol, tf)
        if "BUY" in signal:
            usdt = config["max_usdt_per_trade"]
            amount = usdt / current_price
            record_trade({"type": "BUY", "symbol": symbol, "price": current_price, "amount": amount, "usdt_amount": usdt, "timestamp": datetime.now().isoformat()})
            update_position(symbol, tf, "BUY", current_price, amount)
            pos["entry_price"] = current_price
            send_signal_message("BUY", coin, current_price, last_rsi, last_lower, vol_multiplier, "🟢", "Virtual Buy Executed")
        else:
            sell_fraction = 1.0 if "FULL" in signal or "STOP" in signal else 0.5 if "PARTIAL" in signal else 0.3 if "30" in signal else 0.2
            amount_sold = float(pos["amount"]) * sell_fraction
            received = current_price * amount_sold * (1 - config.get("slippage_percent", 1.5) / 100)
            pnl = (current_price - pos.get("entry_price", current_price)) * amount_sold
            record_trade({"type": "SELL", "symbol": symbol, "price": current_price, "amount": amount_sold, "usdt_received": received, "pnl": pnl, "timestamp": datetime.now().isoformat()})
            update_position(symbol, tf, signal, current_price, amount_sold)

    last_ampel = pos.get("last_ampel", "🟡")
    last_rsi_old = pos.get("last_rsi", 45.0)
    unrealized = 0.0
    if has_position and pos.get("entry_price", 0) > 0:
        unrealized = (current_price - pos["entry_price"]) * float(pos["amount"])

    should_send = (signal != "HOLD") or (has_position and (ampel_emoji != last_ampel or abs(last_rsi - last_rsi_old) > 15))
    reason = "Signal" if signal != "HOLD" else "Position Ampel change" if has_position else "No position"

    if config.get("debug", False):
        print(get_text("debug_ampel_change").format(
            symbol=symbol, old=last_ampel, new=ampel_emoji,
            old_rsi=last_rsi_old, new_rsi=last_rsi,
            send=should_send, reason=reason
        ))

    if should_send:
        send_signal_message(signal, coin, current_price, last_rsi, last_lower, vol_multiplier, ampel_emoji, ampel_text)

    pos["last_ampel"] = ampel_emoji
    pos["last_rsi"] = last_rsi

    history = load_trade_history()
    pos_info = f" | Pos: {float(pos.get('amount', 0)):.2f} | Unrealized: ${unrealized:.1f}" if has_position else " | No position"
    print(f"{symbol} → {signal} | RSI: {last_rsi:.1f} | Vol: {vol_multiplier:.2f}x | Ampel: {ampel_emoji} {ampel_text}{pos_info} | Bal: ${history.get('virtual_balance', 0):.0f} | RealPnL: ${history.get('realized_pnl', 0):.1f}\n")

    return signal
