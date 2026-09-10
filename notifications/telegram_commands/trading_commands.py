from core.config import get_bot_config
from data_manager import list_coins
from notifications.telegram_commands.usage_hints import hint
from notifications.telegram_commands.utils import safe_float, safe_int
from price_fetcher import get_prices, get_prices_batch
from services.trading_service import TradingService
from notifications.telegram_commands.position_display import (
    chunk_positions_message,
    format_sell_list_message,
    format_sell_pick_button_label,
    long_lots_for_sell,
    position_symbol,
    resolve_position_by_display_index,
    resolve_position_by_symbol,
    sort_positions_by_value,
)
from notifications.telegram_commands.manual_order_flow import (
    request_buy_confirmation,
    request_sell_confirmation,
)
from notifications.telegram_commands.watchlist_commands import (
    _coin_symbol,
    format_buy_list_message,
    resolve_coin_by_display_index,
)
from strategies.positions import get_position, list_active_positions
from notifications.telegram_commands.command_context import (
    activate_command,
    clear_context,
    get_context,
    parse_sell_percent_token,
    set_chat_id,
    set_context,
)
from notifications.telegram_i18n import t
from telegram_notifier import answer_callback_query, send_telegram_buttons, send_telegram_message

# Portfolio snapshot after manual buy/sell is sent by TradingService.execute_order.

_trading = TradingService()

SELL_PCT_CALLBACK_PREFIX = "sellpct:"
SELL_POS_CALLBACK_PREFIX = "sellpos:"
_SELL_PCT_PRESETS = (25, 50, 75, 100)


def _sell_menu_text(field: str, **kwargs) -> str:
    from notifications.telegram_commands.menu_i18n import _command_entry, _pack, current_language

    template = str(_command_entry(_pack(current_language()), "sell").get(field) or "")
    for key, value in kwargs.items():
        template = template.replace(f"{{{key}}}", str(value))
    return template


def _sell_position_button_rows(longs: list, prices: dict) -> list:
    """One inline button per long, same 1-based order as the numbered compact list."""
    rows = []
    for index, p in enumerate(sort_positions_by_value(longs, prices), start=1):
        px = float(prices.get(position_symbol(p), 0) or 0)
        rows.append([{
            "text": format_sell_pick_button_label(p, px),
            "callback_data": f"{SELL_POS_CALLBACK_PREFIX}{index}",
        }])
    return rows


def _send_chunked_sell_list(chunks: list[str], buttons: list) -> None:
    """Attach the position keyboard only to the last chunk (never to earlier pages)."""
    if not chunks:
        return
    last = len(chunks) - 1
    for i, chunk in enumerate(chunks):
        if i == last and buttons:
            send_telegram_buttons(chunk, buttons)
        else:
            send_telegram_message(chunk)


def _continue_sell_after_position(p: dict, prices: dict, arg: str, pct_token: str | None = None) -> bool:
    """Shared follow-up after a position is resolved (typed number/symbol or inline button)."""
    sym = position_symbol(p)
    tf = p.get("timeframe") or "4h"
    price = prices.get(sym) or get_prices(sym)[0]
    if not price or price <= 0:
        send_telegram_message(t("price_fetch_failed", sym=sym))
        return True

    pos = get_position(sym, tf)
    total_amount = float(pos.get("amount", 0))
    if total_amount <= 0:
        send_telegram_message(t("no_sellable_amount", sym=sym, tf=tf))
        return True

    if pct_token is None:
        ticker = sym.split("/")[0]
        pos_token = arg if arg.replace(".", "").isdigit() else arg.upper()
        prompt_sell_percentage(ticker)
        activate_command(
            "sell",
            state="sell_awaiting_pct",
            position=pos_token,
            label=ticker,
        )
        return True

    raw_pct = safe_float(pct_token)
    if raw_pct is None or raw_pct <= 0 or raw_pct > 100:
        send_telegram_message(t("invalid_sell_pct"))
        return True
    pct = raw_pct / 100
    amount_sold = total_amount * pct
    if amount_sold <= 0:
        send_telegram_message(t("no_sellable_amount", sym=sym, tf=tf))
        return True

    request_sell_confirmation(
        _trading,
        symbol=sym,
        timeframe=tf,
        price=price,
        amount=amount_sold,
        pct=pct,
    )
    return True


def prompt_sell_percentage(position_label: str, *, invalid: bool = False) -> None:
    """Ask for a sell percentage with 25/50/75/100% quick-reply buttons."""
    prompt = _sell_menu_text("pct_prompt", position=position_label)
    if invalid:
        prompt = _sell_menu_text("pct_invalid") + "\n\n" + prompt
    buttons = [[
        {
            "text": _sell_menu_text(f"pct_btn_{n}"),
            "callback_data": f"{SELL_PCT_CALLBACK_PREFIX}{n}",
        }
        for n in _SELL_PCT_PRESETS
    ]]
    send_telegram_buttons(prompt, buttons)


def handle(text: str) -> bool:
    if text == "/buy":
        coins = list_coins()
        if not coins:
            send_telegram_message(t("watchlist_empty"))
            return True
        symbols = [_coin_symbol(c) for c in coins]
        prices = get_prices_batch(symbols)
        default_usdt = get_bot_config().max_usdt_per_trade
        activate_command("buy", default_usdt=default_usdt)
        send_telegram_message(format_buy_list_message(coins, prices))
        return True

    if text.startswith("/buy "):
        parts = [p.strip() for p in text.split() if p.strip()]
        coins = list_coins()
        if len(parts) < 2:
            send_telegram_message(hint("buy"))
            return True

        sym = None
        if parts[1].replace(".", "").isdigit():
            idx = safe_int(parts[1]) - 1
            coin = resolve_coin_by_display_index(coins, idx)
            if coin:
                sym = coin["symbol"]
            else:
                send_telegram_message(t("invalid_number_buy"))
                return True
        else:
            sym = (parts[1].upper() + "/USDT") if len(parts) > 1 else None

        usdt = safe_float(parts[2]) if len(parts) > 2 else get_bot_config().max_usdt_per_trade
        if not sym or usdt is None or usdt <= 0:
            send_telegram_message(hint("buy"))
            return True

        # WQE-R4: warn (soft/shadow) or block (enforce) manual buys
        try:
            from services.watchlist_quality.config import wqe_mode
            from services.watchlist_quality.enforce import buy_allowed
            from services.watchlist_quality.store import load_quality_scores

            cfg = get_bot_config().raw
            mode = wqe_mode(cfg)
            if mode in ("shadow", "soft", "enforce"):
                data = load_quality_scores()
                scored = next(
                    (c for c in (data.get("coins") or []) if c.get("symbol") == sym),
                    {"symbol": sym},
                )
                ok, reason = buy_allowed(
                    sym,
                    scored_row=scored,
                    config=cfg,
                    source="manual_telegram",
                    is_new_add=True,
                )
                q = scored.get("quality_shadow_ai")
                if q is None:
                    q = scored.get("quality_score")
                if mode == "enforce" and not ok:
                    send_telegram_message(
                        f"❌ WQE block <code>{sym}</code>: {reason}"
                        + (f" (score={q})" if q is not None else "")
                    )
                    return True
                if mode in ("shadow", "soft") and (
                    not ok or (q is not None and float(q) < 0.4)
                ):
                    send_telegram_message(
                        f"⚠️ WQE hint <code>{sym}</code>: {reason if not ok else 'low_score'}"
                        + (f" score={q}" if q is not None else "")
                        + " — fortfahren möglich"
                    )
        except Exception:
            pass

        price = get_prices(sym)[0]
        if price and price > 0:
            request_buy_confirmation(_trading, symbol=sym, timeframe="4h", price=price, usdt=usdt)
        else:
            send_telegram_message(t("price_fetch_failed_check", sym=sym))
        return True

    if text.startswith("/sell"):
        parts = [p.strip() for p in text.split() if p.strip()]
        all_lots = list_active_positions()
        active = long_lots_for_sell(all_lots)
        if len(parts) == 1:
            if not all_lots:
                send_telegram_message(t("no_positions_sell"))
                return True
            symbols = [position_symbol(p) for p in all_lots]
            prices = get_prices_batch(symbols)
            activate_command("sell", state="sell_awaiting_position")
            chunks = chunk_positions_message(
                format_sell_list_message(all_lots, prices),
                annotate_pages=False,
            )
            _send_chunked_sell_list(chunks, _sell_position_button_rows(active, prices))
            return True

        if not all_lots:
            send_telegram_message(t("no_positions_sell"))
            return True

        arg = parts[1]
        has_pct = len(parts) > 2

        symbols = [position_symbol(p) for p in all_lots]
        prices = get_prices_batch(symbols)

        if arg.replace(".", "").isdigit():
            idx = safe_int(arg) - 1
            if idx is None:
                send_telegram_message(hint("sell"))
                return True
            p = resolve_position_by_display_index(active, prices, idx)
        else:
            p = resolve_position_by_symbol(all_lots, arg, prices)
            if p is not None:
                try:
                    from strategies.short_math import is_short

                    if is_short(p):
                        send_telegram_message(t("sell_is_short_use_cover", arg=arg.upper()))
                        return True
                except Exception:
                    pass
                if p not in active and p.get("symbol") not in {x.get("symbol") for x in active}:
                    send_telegram_message(t("sell_is_short_use_cover", arg=arg.upper()))
                    return True

        if not p:
            send_telegram_message(t("no_open_position", arg=arg.upper()))
            return True

        if not has_pct:
            return _continue_sell_after_position(p, prices, arg)
        return _continue_sell_after_position(p, prices, arg, pct_token=parts[2])

    return False


def _handle_sell_pct_callback(callback_query: dict) -> bool:
    from logger import log

    answer_callback_query(callback_query.get("id"))
    data = str(callback_query.get("data") or "")
    raw = data.split(":", 1)[1] if ":" in data else ""

    message = callback_query.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    if not chat_id:
        log("sellpct callback missing chat id", "WARNING")
        return True

    set_chat_id(chat_id)
    entry = get_context(chat_id)
    meta = (entry or {}).get("meta") or {}
    if not entry or entry.get("command") != "sell" or str(meta.get("state") or "") != "sell_awaiting_pct":
        send_telegram_message(_sell_menu_text("pct_expired"))
        return True

    position = str(meta.get("position") or "").strip()
    label = str(meta.get("label") or position)
    canonical = parse_sell_percent_token(raw)
    if not position or canonical is None:
        prompt_sell_percentage(label, invalid=True)
        set_context(chat_id, "sell", **meta)
        return True

    from notifications.telegram_commands.router import dispatch_command

    clear_context(chat_id)
    return dispatch_command(f"/sell {position} {canonical}")


def _handle_sell_pos_callback(callback_query: dict) -> bool:
    from logger import log

    answer_callback_query(callback_query.get("id"))
    data = str(callback_query.get("data") or "")
    raw = data.split(":", 1)[1] if ":" in data else ""

    message = callback_query.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    if not chat_id:
        log("sellpos callback missing chat id", "WARNING")
        return True

    set_chat_id(chat_id)
    entry = get_context(chat_id)
    meta = (entry or {}).get("meta") or {}
    if (
        not entry
        or entry.get("command") != "sell"
        or str(meta.get("state") or "") != "sell_awaiting_position"
    ):
        send_telegram_message(_sell_menu_text("pct_expired"))
        return True

    idx = safe_int(raw)
    if idx is None or idx < 1:
        send_telegram_message(_sell_menu_text("pct_expired"))
        return True

    all_lots = list_active_positions()
    active = long_lots_for_sell(all_lots)
    symbols = [position_symbol(p) for p in all_lots]
    prices = get_prices_batch(symbols)
    p = resolve_position_by_display_index(active, prices, idx - 1)
    if not p:
        send_telegram_message(t("no_open_position", arg=str(idx)))
        return True

    return _continue_sell_after_position(p, prices, str(idx))


def handle_callback(callback_query: dict) -> bool:
    data = str(callback_query.get("data") or "")
    if data.startswith(SELL_POS_CALLBACK_PREFIX):
        return _handle_sell_pos_callback(callback_query)
    if data.startswith(SELL_PCT_CALLBACK_PREFIX):
        return _handle_sell_pct_callback(callback_query)

    from notifications.telegram_commands.manual_order_flow import handle_callback as handle_manual_callback

    return handle_manual_callback(callback_query)
