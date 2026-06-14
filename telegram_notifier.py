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


def _sell_label(signal: str) -> str:
    if "STOP_FULL" in signal or signal.endswith("_FULL"):
        return "100%"
    if "STOP_PARTIAL" in signal or "PARTIAL_50" in signal:
        return "50%"
    if "TP" in signal.upper():
        return "TP 30%"
    if "30" in signal:
        return "30%"
    if "20" in signal:
        return "20%"
    if "STOP" in signal:
        return "STOP"
    return "PARTIAL"


def _mode_badge() -> str:
    from core.config import get_bot_config

    cfg = get_bot_config()
    mode = cfg.trading_mode
    if mode == "live":
        dry = cfg.live_config.get("dry_run", True)
        if not cfg.live_confirmed:
            return "🟠 LIVE (unconfirmed)"
        return "🔶 LIVE DRY" if dry else "🔴 LIVE"
    if mode == "off":
        return "⏸️ OFF"
    return "📋 PAPER"


def send_signal_message(
    signal,
    coin,
    current_price,
    rsi,
    lower_bb,
    vol_multiplier,
    ampel_emoji=None,
    ampel_text=None,
    executed=None,
    trade_message=None,
    trade_result=None,
    sources=None,
    timeframe="4h",
    why_de=None,
    tech_line=None,
    source_de=None,
    social_lines=None,
    confidence=None,
):
    symbol = coin.get("symbol", "Unknown")
    name = coin.get("name", symbol)
    tf = timeframe or coin.get("timeframe", "4h")
    mode_badge = _mode_badge()
    from notifications.user_explain import explanations_config, explain_risk

    exp_cfg = explanations_config()
    source_line = ""
    if source_de:
        source_line = f"\n<b>Quellen:</b> {source_de}"
    elif sources:
        source_line = f"\n<b>Sources:</b> {', '.join(sources)}"

    if signal == "BUY":
        emoji = "🟢"
        if executed is True:
            title = "BUY EXECUTED"
        elif executed is False:
            title = "BUY BLOCKED"
        else:
            title = "BUY SIGNAL"
        pos = get_position(symbol, tf)
        amount = float(trade_result.amount) if trade_result and trade_result.executed else float(pos.get("amount", 0))
        cost = (trade_result.usdt_amount if trade_result and trade_result.executed else current_price * amount) if current_price > 0 else 0
        extra = f"\n<b>Amount:</b> {amount:.4f} | <b>Cost:</b> ${cost:.1f}"
    elif "SELL" in signal:
        emoji = "🔴"
        pct = _sell_label(signal)
        if executed is True:
            title = f"SELL {pct} EXECUTED"
        elif executed is False:
            title = f"SELL {pct} BLOCKED"
        else:
            title = f"SELL {pct} SIGNAL"
        pos = get_position(symbol, tf)
        entry = float(pos.get("average_entry", 0))
        sold_amount = float(trade_result.amount) if trade_result and trade_result.executed else 0.0
        pnl = float(trade_result.pnl) if trade_result and trade_result.executed else 0.0
        extra = ""
        if entry > 0:
            extra += f"\n<b>Entry:</b> ${entry:.4f}"
        if sold_amount > 0:
            extra += f"\n<b>Sold:</b> {sold_amount:.4f}"
        if pnl != 0:
            extra += f"\n<b>PnL:</b> ${pnl:+.2f}"
    else:
        emoji = "📡"
        title = "MARKET UPDATE"
        extra = f"\n<b>Ampel:</b> {ampel_text}" if ampel_text else ""

    ampel_line = f"<b>Ampel:</b> {ampel_emoji} {ampel_text}\n" if ampel_emoji and ampel_emoji != "📡" else ""
    from price_fetcher import format_usdt_price

    price_str = (
        format_usdt_price(float(current_price)).replace("$", "")
        if isinstance(current_price, (int, float)) and current_price > 0
        else "—"
    )
    rsi_str = f"{rsi:.1f}" if isinstance(rsi, (int, float)) and rsi > 0 else "—"

    why_line = f"\n<b>Warum:</b> {why_de}" if why_de and exp_cfg.get("enabled", True) else ""
    conf_line = f"\n<b>Confidence:</b> {confidence:.0f}%" if isinstance(confidence, (int, float)) and confidence > 0 else ""
    social_block = ""
    if social_lines:
        social_block = "\n" + "\n".join(f"<b>Social:</b> {line}" if i == 0 else line for i, line in enumerate(social_lines))

    if executed is False and trade_message:
        risk_de = explain_risk(trade_message) if exp_cfg.get("enabled", True) else trade_message
        blocked_line = f"\n<b>Grund:</b> {risk_de}"
    else:
        blocked_line = ""
    exec_line = f"\n<b>Fill:</b> {trade_message}" if executed is True and trade_message else ""
    tech_block = f"\n<code>{tech_line}</code>" if tech_line and exp_cfg.get("show_technical_codes", True) else ""

    message = f"""
{emoji} <b>{title}</b> — {symbol}
<b>Mode:</b> {mode_badge}

<b>Name:</b> {name}
<b>Preis:</b> ${price_str}
<b>RSI:</b> {rsi_str}
{ampel_line}{why_line}{conf_line}{source_line}{social_block}{extra}{blocked_line}{exec_line}{tech_block}
🕒 {datetime.now().strftime("%H:%M:%S")}
"""
    send_telegram_message(message.strip())


def send_hold_explanation_message(symbol: str, why_de: str, tech_line: str = ""):
    from notifications.user_explain import explanations_config

    cfg = explanations_config()
    if not cfg.get("enabled") or not cfg.get("notify_social_hold_explanations"):
        return False
    tech_block = f"\n<code>{tech_line}</code>" if tech_line and cfg.get("show_technical_codes", True) else ""
    msg = (
        f"👀 <b>Kein Trade</b> — {symbol}\n"
        f"<b>Warum:</b> {why_de}{tech_block}\n"
        f"🕒 {datetime.now().strftime('%H:%M:%S')}"
    )
    return send_telegram_message(msg)


def send_cmc_cycle_digest(signals: list):
    from notifications.user_explain import explain_cmc_signal, explanations_config

    cfg = explanations_config()
    if not cfg.get("enabled") or not cfg.get("notify_cmc_digest"):
        return False
    min_conf = int(cfg.get("cmc_digest_min_confidence", 60))
    filtered = [s for s in signals if getattr(s, "confidence", 0) >= min_conf]
    if not filtered:
        return False
    lines = [f"<b>📊 CMC diesen Zyklus</b> — {datetime.now().strftime('%H:%M:%S')}", ""]
    for s in filtered[:8]:
        lines.append(explain_cmc_signal(s))
        lines.append("")
    return send_telegram_message("\n".join(lines).strip())


def send_x_cycle_digest(signals: list, skip_post_ids: set = None):
    from notifications.user_explain import explain_x_signal, explanations_config

    cfg = explanations_config()
    if not cfg.get("enabled") or not cfg.get("notify_x_digest"):
        return False
    min_eff = float(cfg.get("x_digest_min_effective_confidence", 70))
    skip = skip_post_ids or set()
    filtered = []
    for s in signals:
        eff = getattr(s, "effective_confidence", getattr(s, "confidence", 0))
        pid = getattr(s, "post_id", None)
        if eff >= min_eff and pid not in skip:
            filtered.append(s)
    if not filtered:
        return False
    lines = [f"<b>🐦 X-Signale diesen Zyklus</b> — {datetime.now().strftime('%H:%M:%S')}", ""]
    for s in filtered[:6]:
        lines.append(explain_x_signal(s))
        lines.append("")
    return send_telegram_message("\n".join(lines).strip())


def send_x_recommendation_message(recommendation):
    """Clean message for X recommendations with raw tweet and rationale."""
    emoji = "🟢" if recommendation["action"] == "BUY" else "🔴" if recommendation["action"] == "SELL" else "📋" if recommendation["action"] == "ADD_TO_WATCHLIST" else "⏸️"
    title = recommendation["action"]
    raw = recommendation.get("raw_tweet", "—")[:100] + "..." if len(recommendation.get("raw_tweet", "")) > 100 else recommendation.get("raw_tweet", "—")
    tp = recommendation.get("price_target")
    sl = recommendation.get("stop_loss")
    target_lines = ""
    if tp is not None:
        target_lines += f"\n<b>Take Profit:</b> ${float(tp):.4f}"
    if sl is not None:
        target_lines += f"\n<b>Stop Loss:</b> ${float(sl):.4f}"

    act_de = "Kauf" if title == "BUY" else "Verkauf" if title == "SELL" else title
    msg = f"""{emoji} <b>{title} EMPFEHLUNG</b> — {recommendation.get("coin", "UNKNOWN")}/USDT

<b>Von:</b> @{recommendation.get("account", "Unknown")}
<b>Empfehlung:</b> {act_de}
<b>Tweet:</b> {raw}
<b>Confidence:</b> {recommendation.get("confidence", 0)}% | Trust: {recommendation.get("trust_at_signal", "—")}
<b>Warum:</b> {recommendation.get("rationale", "—")}{target_lines}

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
