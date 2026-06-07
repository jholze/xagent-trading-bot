import os
from datetime import datetime

import requests

from data_manager import is_demo_mode
from strategies.positions import get_position
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


def send_cycle_summary(text: str):
    """Send end-of-cycle summary (respects notify_on_cycle config)."""
    from data_manager import get_config
    if not get_config().get("observability", {}).get("notify_on_cycle", False):
        return False
    return send_telegram_message(text)


def send_telegram_message(text, reply_markup=None):
    if not BOT_TOKEN or not CHAT_ID:
        print("⚠️ Telegram not configured")
        return False

    if is_demo_mode():
        text = "🧪 [DEMO] " + text

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        response = requests.post(url, json=payload, timeout=10)
        return response.status_code == 200
    except Exception as e:
        print(f"Error sending Telegram message: {e}")
        return False


def send_telegram_buttons(text, buttons):
    """buttons: list of rows, each row is list of {text, callback_data} dicts."""
    reply_markup = {"inline_keyboard": buttons}
    return send_telegram_message(text, reply_markup=reply_markup)


def answer_callback_query(callback_id, text=None):
    if not BOT_TOKEN:
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery"
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    try:
        response = requests.post(url, json=payload, timeout=10)
        return response.status_code == 200
    except Exception as e:
        print(f"Error answering callback query: {e}")
        return False


def handle_telegram_command(text):
    """Delegates to modular command router."""
    from notifications.telegram_commands.router import dispatch_command
    return dispatch_command(text)


def handle_telegram_callback(callback_query):
    from notifications.telegram_commands.router import dispatch_callback
    return dispatch_callback(callback_query)
