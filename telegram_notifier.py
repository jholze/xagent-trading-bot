import os
import requests

from core.runtime_identity import message_prefix
from core.time_utils import format_display_hms
from strategies.positions import get_position
from logger import log

_BOT_USERNAME_CACHE: str | None = None


def _bot_token() -> str | None:
    return os.getenv("TELEGRAM_BOT_TOKEN")


def get_bot_username() -> str:
    """Cached @username from Telegram getMe (for invite deep links)."""
    global _BOT_USERNAME_CACHE
    if _BOT_USERNAME_CACHE:
        return _BOT_USERNAME_CACHE
    token = _bot_token()
    if not token:
        return ""
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{token}/getMe",
            timeout=10,
        )
        if resp.status_code == 200:
            body = resp.json()
            if body.get("ok"):
                _BOT_USERNAME_CACHE = str((body.get("result") or {}).get("username") or "")
    except Exception as e:
        log(f"getMe failed: {e}", "WARNING")
    return _BOT_USERNAME_CACHE or ""


def build_tenant_invite_link(tenant_id: str) -> str:
    uname = get_bot_username()
    tid = (tenant_id or "").strip().lower()
    if uname:
        return f"https://t.me/{uname}?start={tid}"
    return f"/start {tid}"


def _env_chat_id() -> str | None:
    return os.getenv("TELEGRAM_CHAT_ID")


def _chat_id() -> str | None:
    """Notification target: tenant owner chat when in tenant context, else operator env."""
    try:
        from core.tenant_context import current_tenant_context

        ctx = current_tenant_context()
        if ctx and ctx.owner_chat_id:
            return str(ctx.owner_chat_id)
    except Exception:
        pass
    return _env_chat_id()


def _headless_tenant_tag() -> str:
    """Tag messages from a headless tenant sharing the operator's chat, so several
    such tenants stay distinguishable in one inbox."""
    try:
        from core.tenant_context import DEFAULT_TENANT, current_tenant_context

        ctx = current_tenant_context()
        if ctx and ctx.headless and ctx.tenant_id and ctx.tenant_id != DEFAULT_TENANT:
            return f"[{ctx.tenant_id}] "
    except Exception:
        pass
    return ""

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
        pnl = None
        if trade_result and trade_result.executed and trade_result.pnl is not None:
            pnl = float(trade_result.pnl)
        extra = ""
        if entry > 0:
            extra += f"\n<b>Entry:</b> ${entry:.4f}"
        if sold_amount > 0:
            extra += f"\n<b>Sold:</b> {sold_amount:.4f}"
        if pnl is not None:
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

    from notifications.coin_links import format_links_line, format_ticker_html, inline_link_buttons

    ticker = symbol.split("/")[0] if "/" in symbol else symbol
    symbol_html = format_ticker_html(ticker, name=name)
    links_line = format_links_line(ticker, name=name)
    links_block = f"\n{links_line}" if links_line else ""

    message = f"""
{emoji} <b>{title}</b> — {symbol_html}
<b>Mode:</b> {mode_badge}
{links_block}

<b>Name:</b> {name}
<b>Preis:</b> ${price_str}
<b>RSI:</b> {rsi_str}
{ampel_line}{why_line}{conf_line}{source_line}{social_block}{extra}{blocked_line}{exec_line}{tech_block}
🕒 {format_display_hms()}
"""
    buttons = inline_link_buttons(ticker, name=name)
    reply_markup = {"inline_keyboard": buttons} if buttons else None
    send_telegram_message(message.strip(), reply_markup=reply_markup)

    if executed is True:
        from notifications.chart_image import send_trade_chart_if_enabled

        send_trade_chart_if_enabled(
            symbol,
            executed=True,
            current_price=float(current_price) if current_price else None,
            reply_markup=reply_markup,
        )


def send_hold_explanation_message(symbol: str, why_de: str, tech_line: str = ""):
    from notifications.user_explain import explanations_config

    cfg = explanations_config()
    if not cfg.get("enabled") or not cfg.get("notify_social_hold_explanations"):
        return False
    tech_block = f"\n<code>{tech_line}</code>" if tech_line and cfg.get("show_technical_codes", True) else ""
    from notifications.coin_links import format_links_line, format_ticker_html, inline_link_buttons

    ticker = symbol.split("/")[0] if "/" in symbol else symbol
    symbol_html = format_ticker_html(ticker)
    links_line = format_links_line(ticker)
    links_block = f"\n{links_line}" if links_line else ""
    msg = (
        f"👀 <b>Kein Trade</b> — {symbol_html}\n"
        f"{links_block}\n"
        f"<b>Warum:</b> {why_de}{tech_block}\n"
        f"🕒 {format_display_hms()}"
    )
    buttons = inline_link_buttons(ticker)
    reply_markup = {"inline_keyboard": buttons} if buttons else None
    return send_telegram_message(msg.strip(), reply_markup=reply_markup)


def send_cmc_cycle_digest(signals: list):
    from notifications.user_explain import explain_cmc_signal, explanations_config

    cfg = explanations_config()
    if not cfg.get("enabled") or not cfg.get("notify_cmc_digest"):
        return False
    min_conf = int(cfg.get("cmc_digest_min_confidence", 60))
    filtered = [
        s for s in signals
        if getattr(s, "confidence", 0) >= min_conf
        and getattr(s, "signal_tier", "") != "quote"
    ]
    if not filtered:
        return False
    max_coins = int(cfg.get("cmc_digest_max_coins", 5))
    lines = [
        f"<b>📊 CMC-Zyklus</b> — Beobachtung (kein Trade ohne ✅ EXECUTED)",
        format_display_hms(),
        "",
    ]
    for s in filtered[:max_coins]:
        lines.append(explain_cmc_signal(s))
        lines.append("")
    from bus.schemas import PRIORITY_CYCLE
    return send_telegram_message("\n".join(lines).strip(), priority=PRIORITY_CYCLE)


def send_lc_cycle_digest(signals: list):
    from notifications.user_explain import explain_lc_signal, explanations_config

    cfg = explanations_config()
    if not cfg.get("enabled") or not cfg.get("notify_lc_digest"):
        return False
    min_conf = int(cfg.get("lc_digest_min_confidence", 55))
    filtered = [s for s in signals if getattr(s, "confidence", 0) >= min_conf]
    if not filtered:
        return False
    lines = [f"<b>🌙 LunarCrush diesen Zyklus</b> — {format_display_hms()}", ""]
    for s in filtered[:8]:
        lines.append(explain_lc_signal(s))
        lines.append("")
    from bus.schemas import PRIORITY_CYCLE
    return send_telegram_message("\n".join(lines).strip(), priority=PRIORITY_CYCLE)


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
    lines = [f"<b>🐦 X-Signale diesen Zyklus</b> — {format_display_hms()}", ""]
    for s in filtered[:6]:
        lines.append(explain_x_signal(s))
        lines.append("")
    from bus.schemas import PRIORITY_CYCLE
    return send_telegram_message("\n".join(lines).strip(), priority=PRIORITY_CYCLE)


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

    from notifications.coin_links import format_links_line, format_ticker_html, inline_link_buttons

    coin = recommendation.get("coin", "UNKNOWN")
    act_de = "Kauf" if title == "BUY" else "Verkauf" if title == "SELL" else title
    symbol_html = format_ticker_html(coin)
    links_line = format_links_line(coin)
    links_block = f"{links_line}\n\n" if links_line else ""
    msg = f"""{emoji} <b>{title} EMPFEHLUNG</b> — {symbol_html}/USDT

{links_block}<b>Von:</b> @{recommendation.get("account", "Unknown")}
<b>Empfehlung:</b> {act_de}
<b>Tweet:</b> {raw}
<b>Confidence:</b> {recommendation.get("confidence", 0)}% | Trust: {recommendation.get("trust_at_signal", "—")}
<b>Warum:</b> {recommendation.get("rationale", "—")}{target_lines}

🕒 {format_display_hms()}
"""
    buttons = inline_link_buttons(coin)
    reply_markup = {"inline_keyboard": buttons} if buttons else None
    send_telegram_message(msg.strip(), reply_markup=reply_markup)


def send_merged_social_digest(
    cmc_signals: list,
    lc_signals: list,
    x_signals: list,
    *,
    skip_post_ids: set | None = None,
):
    """Single Telegram message for CMC + LC + X cycle digests."""
    from notifications.user_explain import (
        explain_cmc_signal,
        explain_lc_signal,
        explain_x_signal,
        explanations_config,
    )

    cfg = explanations_config()
    if not cfg.get("enabled"):
        return False

    skip = skip_post_ids or set()
    sections: list[str] = []

    if cfg.get("notify_cmc_digest"):
        min_conf = int(cfg.get("cmc_digest_min_confidence", 60))
        cmc_filtered = [
            s for s in cmc_signals
            if getattr(s, "confidence", 0) >= min_conf
            and getattr(s, "signal_tier", "") != "quote"
        ]
        if cmc_filtered:
            max_coins = int(cfg.get("cmc_digest_max_coins", 5))
            lines = ["<b>📊 CMC</b>"]
            for s in cmc_filtered[:max_coins]:
                lines.append(explain_cmc_signal(s))
            sections.append("\n".join(lines))

    if cfg.get("notify_lc_digest"):
        min_conf = int(cfg.get("lc_digest_min_confidence", 55))
        lc_filtered = [s for s in lc_signals if getattr(s, "confidence", 0) >= min_conf]
        if lc_filtered:
            lines = ["<b>🌙 LunarCrush</b>"]
            for s in lc_filtered[:8]:
                lines.append(explain_lc_signal(s))
            sections.append("\n".join(lines))

    if cfg.get("notify_x_digest"):
        min_eff = float(cfg.get("x_digest_min_effective_confidence", 70))
        x_filtered = []
        for s in x_signals:
            eff = getattr(s, "effective_confidence", getattr(s, "confidence", 0))
            pid = getattr(s, "post_id", None)
            if eff >= min_eff and pid not in skip:
                x_filtered.append(s)
        if x_filtered:
            lines = ["<b>🐦 X</b>"]
            for s in x_filtered[:6]:
                lines.append(explain_x_signal(s))
            sections.append("\n".join(lines))

    if not sections:
        return False

    header = f"<b>📡 Social diesen Zyklus</b> — {format_display_hms()}\n<i>Beobachtung — kein Trade ohne ✅ EXECUTED</i>"
    body = "\n\n".join(sections)
    from bus.schemas import PRIORITY_CYCLE
    return send_telegram_message(f"{header}\n\n{body}".strip(), priority=PRIORITY_CYCLE)


def send_cycle_summary(text: str, *, cycle_ctx: dict | None = None):
    """Send end-of-cycle summary (notify_on_cycle + optional delta gating)."""
    from data_manager import get_config

    if not get_config().get("observability", {}).get("notify_on_cycle", False):
        return False

    if cycle_ctx is not None:
        from logger import log
        from services.cycle_notification_policy import cycle_notification_policy

        total_value = float(cycle_ctx.get("total_value", 0) or 0)
        coin_results = cycle_ctx.get("coin_results")
        if not cycle_notification_policy.should_send_summary(
            coin_results=coin_results,
            total_value=total_value,
        ):
            log(cycle_notification_policy.skip_reason(
                coin_results=coin_results,
                total_value=total_value,
            ), "DEBUG")
            return False

    from bus.schemas import PRIORITY_CYCLE
    return send_telegram_message(text, priority=PRIORITY_CYCLE)


def send_telegram_photo(caption: str, photo_path: str, reply_markup=None) -> bool:
    bot_token = _bot_token()
    chat_id = _chat_id()
    if not bot_token or not chat_id:
        print("⚠️ Telegram not configured")
        return False

    prefix = message_prefix()
    if prefix:
        caption = prefix + caption

    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    try:
        with open(photo_path, "rb") as photo_file:
            payload = {
                "chat_id": chat_id,
                "caption": caption[:1024],
                "parse_mode": "HTML",
            }
            if reply_markup:
                import json

                payload["reply_markup"] = json.dumps(reply_markup)
            response = requests.post(url, data=payload, files={"photo": photo_file}, timeout=20)
        return response.status_code == 200
    except Exception as e:
        print(f"Error sending Telegram photo: {e}")
        return False


def resolve_notification_chat_id(chat_id: str | int | None = None) -> str:
    if chat_id is not None and str(chat_id).strip():
        return str(chat_id).strip()
    return str(_chat_id() or "").strip()


def _send_telegram_direct(text, reply_markup=None, *, chat_id: str | int | None = None, parse_mode: str = "HTML"):
    """Synchronous Telegram HTTP send (used by notification worker)."""
    bot_token = _bot_token()
    target_chat = resolve_notification_chat_id(chat_id)
    if not bot_token or not target_chat:
        print("⚠️ Telegram not configured")
        return False

    prefix = message_prefix() + _headless_tenant_tag()
    if prefix:
        text = prefix + text

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": target_chat, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code != 200:
            log(f"Telegram send HTTP {response.status_code}: {response.text[:200]}", "WARNING")
            return False
        body = response.json()
        if not body.get("ok"):
            log(f"Telegram send failed: {body.get('description', body)}", "WARNING")
            return False
        return True
    except Exception as e:
        log(f"Error sending Telegram message: {e}", "WARNING")
        return False


def send_telegram_message(
    text,
    reply_markup=None,
    *,
    chat_id: str | int | None = None,
    parse_mode: str = "HTML",
    priority: int | None = None,
):
    from bus.schemas import PRIORITY_CYCLE, PRIORITY_URGENT
    from core.config import get_bot_config

    prio = PRIORITY_URGENT if priority is None else int(priority)
    cfg = get_bot_config()
    mode = cfg.architecture_config.get("notification_mode", "async")

    if mode == "async" and prio >= PRIORITY_CYCLE:
        try:
            from bus.notifications import notification_publisher
            from services.architecture_runtime import ensure_started

            ensure_started()
            if notification_publisher.running:
                notification_publisher.enqueue(
                    text,
                    priority=prio,
                    chat_id=chat_id if chat_id is not None else resolve_notification_chat_id(),
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                    kind="cycle" if prio >= PRIORITY_CYCLE else "message",
                )
                return True
        except Exception as e:
            log(f"Async notification fallback to sync: {e}", "WARNING")

    return _send_telegram_direct(
        text, reply_markup=reply_markup, chat_id=chat_id, parse_mode=parse_mode
    )


def send_telegram_buttons(text, buttons, *, chat_id: str | int | None = None):
    """buttons: list of rows, each row is list of {text, callback_data} dicts."""
    reply_markup = {"inline_keyboard": buttons}
    return send_telegram_message(text, reply_markup=reply_markup, chat_id=chat_id)


def send_reply_keyboard(
    text,
    rows: list[list[str]],
    *,
    one_time: bool = False,
    chat_id: str | int | None = None,
) -> bool:
    """Persistent section keyboard below the input field (rows of button labels)."""
    target = resolve_notification_chat_id(chat_id)
    if not _bot_token() or not target:
        print("⚠️ Telegram not configured")
        return False

    keyboard = [[{"text": label} for label in row] for row in rows]
    reply_markup = {
        "keyboard": keyboard,
        "resize_keyboard": True,
        "is_persistent": True,
        "one_time_keyboard": one_time,
    }
    return send_telegram_message(text, reply_markup=reply_markup, chat_id=target)


def edit_telegram_message(text, chat_id, message_id, reply_markup=None):
    bot_token = _bot_token()
    if not bot_token or not chat_id or not message_id:
        return False

    prefix = message_prefix()
    if prefix:
        text = prefix + text

    url = f"https://api.telegram.org/bot{bot_token}/editMessageText"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = {"inline_keyboard": reply_markup}

    try:
        response = requests.post(url, json=payload, timeout=10)
        return response.status_code == 200
    except Exception as e:
        print(f"Error editing Telegram message: {e}")
        return False


def answer_callback_query(callback_id, text=None):
    bot_token = _bot_token()
    if not bot_token:
        return False
    url = f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery"
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    try:
        response = requests.post(url, json=payload, timeout=10)
        return response.status_code == 200
    except Exception as e:
        print(f"Error answering callback query: {e}")
        return False


def handle_telegram_command(text, chat_id=None):
    """Delegates to modular command router."""
    from notifications.telegram_commands.command_context import clear_context, set_chat_id

    if chat_id is not None:
        set_chat_id(chat_id)
        clear_context(chat_id)
    from notifications.telegram_commands.router import dispatch_command
    return dispatch_command(text)


def handle_telegram_text(text, chat_id=None):
    """Non-slash messages (e.g. section buttons on reply keyboard).

    Also supports the super-simple onboarding flow: operator can paste
    onboarding data (token+key+secret) as a plain private message.
    """
    from notifications.telegram_commands.command_context import try_resolve

    if chat_id is not None and try_resolve(chat_id, text):
        return True

    # Try onboarding handler for plain-text pastes (operator-only, returns False quickly otherwise).
    # This enables the "just send the data as a normal private message" UX.
    from notifications.telegram_commands import onboarding_commands
    if onboarding_commands.handle(text):
        return True

    from notifications.telegram_commands.menu_commands import handle_text

    return handle_text(text, chat_id=chat_id)


def handle_telegram_callback(callback_query):
    from notifications.telegram_commands.router import dispatch_callback
    return dispatch_callback(callback_query)


def set_webhook_for_bot(token: str, tenant_id: str) -> bool:
    """Set Telegram webhook for a user's own bot token (BYOB onboarding)."""
    import json
    import requests

    base = (os.getenv("WEBHOOK_BASE_URL") or os.getenv("RAILWAY_PUBLIC_DOMAIN") or "").strip().rstrip("/")
    if not base:
        log("No WEBHOOK_BASE_URL set — cannot register user webhook", "WARNING")
        return False
    if not base.startswith("http"):
        base = f"https://{base}"

    url = f"{base}/webhook/{tenant_id}"

    # fetch secret if present
    from storage.tenant_registry import get_webhook_secret
    secret = get_webhook_secret(tenant_id, test=False)

    api = f"https://api.telegram.org/bot{token}/setWebhook"
    payload = {
        "url": url,
        "drop_pending_updates": "true",
        "allowed_updates": json.dumps(["message", "callback_query"]),
    }
    if secret:
        payload["secret_token"] = secret

    try:
        resp = requests.post(api, data=payload, timeout=15)
        data = resp.json() if resp.status_code == 200 else {}
        if data.get("ok"):
            log(f"User webhook registered for tenant {tenant_id}: {url}", "INFO")
            return True
        else:
            log(f"setWebhook failed for {tenant_id}: {resp.text[:200]}", "WARNING")
            return False
    except Exception as e:
        log(f"setWebhook exception for {tenant_id}: {e}", "ERROR")
        return False


def send_message_with_bot_token(token: str, chat_id: str | int, text: str) -> bool:
    """Send a message using a foreign bot token (used during onboarding)."""
    import requests
    api = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(
            api,
            data={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        return resp.status_code == 200 and resp.json().get("ok", False)
    except Exception as e:
        log(f"send_message_with_bot_token failed: {e}", "WARNING")
        return False
