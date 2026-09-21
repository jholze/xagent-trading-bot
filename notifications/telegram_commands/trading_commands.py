import time

from core.config import get_bot_config
from data_manager import list_coins
from notifications.telegram_commands.usage_hints import hint
from notifications.telegram_commands.utils import safe_float, safe_int
from price_fetcher import get_prices, get_prices_batch, peek_cached_price
from services.trading_service import TradingService
from notifications.telegram_commands.position_display import (
    build_price_fallbacks,
    chunk_positions_message,
    encode_lot_callback,
    format_sell_list_message,
    format_sell_pick_button_label,
    long_lots_for_sell,
    LOT_SELL_CALLBACK_PREFIX,
    LOT_WHY_CALLBACK_PREFIX,
    parse_lot_callback,
    position_symbol,
    resolve_position_by_display_index,
    resolve_position_by_symbol,
    resolve_position_by_symbol_tf,
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

BUY_COIN_CALLBACK_PREFIX = "buycoin:"
BUY_AMT_CALLBACK_PREFIX = "buyamt:"
BUY_BACK_CALLBACK = "buyback"


def _command_menu_text(command: str, field: str, **kwargs) -> str:
    from notifications.telegram_commands.menu_i18n import _command_entry, _pack, current_language

    template = str(_command_entry(_pack(current_language()), command).get(field) or "")
    for key, value in kwargs.items():
        template = template.replace(f"{{{key}}}", str(value))
    return template


def _sell_menu_text(field: str, **kwargs) -> str:
    return _command_menu_text("sell", field, **kwargs)


def _buy_menu_text(field: str, **kwargs) -> str:
    return _command_menu_text("buy", field, **kwargs)


def _sell_position_button_rows(longs: list, prices: dict) -> list:
    """One inline button per long, same 1-based order as the numbered compact list."""
    rows = []
    for p in sort_positions_by_value(longs, prices):
        px = float(prices.get(position_symbol(p), 0) or 0)
        rows.append([{
            "text": format_sell_pick_button_label(p, px),
            "callback_data": encode_lot_callback(
                SELL_POS_CALLBACK_PREFIX,
                position_symbol(p),
                p.get("timeframe"),
            ),
        }])
    return rows


def _display_prices_for_lots(lots: list) -> dict:
    """Last-known cache + entry fallbacks. Does not hit Gate."""
    prices = {}
    fallbacks = build_price_fallbacks(lots or [])
    for p in lots or []:
        sym = position_symbol(p)
        cached = peek_cached_price(sym)
        try:
            cached_px = float(cached or 0)
        except (TypeError, ValueError):
            cached_px = 0.0
        if cached_px > 0:
            prices[sym] = cached_px
        else:
            fb = float(fallbacks.get(sym, 0) or 0)
            if fb > 0:
                prices[sym] = fb
    return prices


def _live_price_for_symbol(sym: str) -> float:
    """Live-fetch at most this one symbol for the sell confirm preview."""
    batch = get_prices_batch([sym])
    try:
        px = float((batch or {}).get(sym, 0) or 0)
    except (TypeError, ValueError):
        px = 0.0
    if px > 0:
        return px
    return float(get_prices(sym)[0] or 0)


def _log_callback_first_reply(prefix: str, t0: float) -> None:
    from logger import log

    ms = (time.perf_counter() - t0) * 1000.0
    log(f"callback_received prefix={prefix} first_reply_ms={ms:.0f}", "INFO")


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
            timeframe=str(tf),
        )
        return True

    price = _live_price_for_symbol(sym)
    if not price or price <= 0:
        send_telegram_message(t("price_fetch_failed", sym=sym))
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


def _buy_usdt_presets(max_usdt: float) -> list[int]:
    """Positive USDT buttons including the configured default, never auto-selected."""
    try:
        cap = int(round(float(max_usdt or 0)))
    except (TypeError, ValueError):
        cap = 0
    if cap <= 0:
        cap = 25
    steps = (10, 25, 50, 100, 200, 500, 1000)
    out: list[int] = []
    seen: set[int] = set()
    for n in (*[s for s in steps if s < cap], cap):
        if n > 0 and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def format_buy_pick_button_label(coin: dict, price: float) -> str:
    from price_fetcher import format_usdt_price

    ticker = _coin_symbol(coin).split("/")[0]
    if price and price > 0:
        return f"{ticker} {format_usdt_price(price)}"
    return ticker


def _buy_coin_button_rows(coins: list, prices: dict) -> list:
    rows = []
    for index, coin in enumerate(coins, start=1):
        px = float(prices.get(_coin_symbol(coin), 0) or 0)
        rows.append([{
            "text": format_buy_pick_button_label(coin, px),
            "callback_data": f"{BUY_COIN_CALLBACK_PREFIX}{index}",
        }])
    return rows


def prompt_buy_amount(coin_label: str, *, invalid: bool = False, max_usdt: float | None = None) -> None:
    """Ask for a buy size. Presets include max_usdt_per_trade; nothing is assumed."""
    prompt = _buy_menu_text("usdt_prompt", coin=coin_label)
    if invalid:
        prompt = _buy_menu_text("usdt_invalid") + "\n\n" + prompt
    if max_usdt is None:
        max_usdt = get_bot_config().max_usdt_per_trade
    presets = _buy_usdt_presets(max_usdt)
    buttons = [[
        {
            "text": _buy_menu_text("usdt_btn", usdt=str(n)),
            "callback_data": f"{BUY_AMT_CALLBACK_PREFIX}{n}",
        }
        for n in presets
    ]]
    buttons.append([{
        "text": _buy_menu_text("back_btn"),
        "callback_data": BUY_BACK_CALLBACK,
    }])
    send_telegram_buttons(prompt, buttons)


def _continue_buy_after_coin(sym: str, arg: str, usdt_token: str | None = None) -> bool:
    """Shared follow-up after a watchlist coin is resolved (typed or inline button)."""
    if usdt_token is None:
        ticker = sym.split("/")[0]
        pos_token = arg if str(arg).replace(".", "").isdigit() else str(arg).upper()
        prompt_buy_amount(ticker)
        activate_command(
            "buy",
            state="buy_awaiting_usdt",
            coin=pos_token,
            label=ticker,
        )
        return True

    usdt = safe_float(usdt_token)
    if usdt is None or usdt <= 0:
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


def handle(text: str) -> bool:
    if text == "/buy":
        coins = list_coins()
        if not coins:
            send_telegram_message(t("watchlist_empty"))
            return True
        symbols = [_coin_symbol(c) for c in coins]
        prices = get_prices_batch(symbols)
        chunks = chunk_positions_message(
            format_buy_list_message(coins, prices),
            annotate_pages=False,
        )
        _send_chunked_sell_list(chunks, _buy_coin_button_rows(coins, prices))
        activate_command("buy", state="buy_awaiting_coin")
        return True

    if text.startswith("/buy "):
        parts = [p.strip() for p in text.split() if p.strip()]
        coins = list_coins()
        if len(parts) < 2:
            send_telegram_message(hint("buy"))
            return True

        arg = parts[1]
        sym = None
        if arg.replace(".", "").isdigit():
            idx = safe_int(arg) - 1
            coin = resolve_coin_by_display_index(coins, idx)
            if coin:
                sym = _coin_symbol(coin)
            else:
                send_telegram_message(t("invalid_number_buy"))
                return True
        else:
            ticker = arg.upper()
            sym = ticker if "/" in ticker else f"{ticker}/USDT"

        if not sym:
            send_telegram_message(hint("buy"))
            return True

        if len(parts) < 3:
            return _continue_buy_after_coin(sym, arg)
        return _continue_buy_after_coin(sym, arg, usdt_token=parts[2])

    if text.startswith("/sell"):
        parts = [p.strip() for p in text.split() if p.strip()]
        all_lots = list_active_positions()
        active = long_lots_for_sell(all_lots)
        if len(parts) == 1:
            if not all_lots:
                send_telegram_message(t("no_positions_sell"))
                return True
            prices = _display_prices_for_lots(all_lots)
            chunks = chunk_positions_message(
                format_sell_list_message(all_lots, prices),
                annotate_pages=False,
            )
            _send_chunked_sell_list(chunks, _sell_position_button_rows(active, prices))
            activate_command("sell", state="sell_awaiting_position")
            return True

        if not all_lots:
            send_telegram_message(t("no_positions_sell"))
            return True

        arg = parts[1]
        has_pct = len(parts) > 2

        prices = _display_prices_for_lots(all_lots)

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

    t0 = time.perf_counter()
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
        _log_callback_first_reply("sellpct", t0)
        return True

    position = str(meta.get("position") or "").strip()
    label = str(meta.get("label") or position)
    timeframe = str(meta.get("timeframe") or "").strip()
    canonical = parse_sell_percent_token(raw)
    if not position or canonical is None:
        prompt_sell_percentage(label, invalid=True)
        set_context(chat_id, "sell", **meta)
        _log_callback_first_reply("sellpct", t0)
        return True

    send_telegram_message(_sell_menu_text("working", position=label, pct=canonical))
    _log_callback_first_reply("sellpct", t0)
    clear_context(chat_id)
    try:
        return _finish_sell_pct_from_context(label, position, timeframe, canonical)
    except Exception as e:
        log(f"sellpct preview failed: {e}", "ERROR")
        send_telegram_message(t("price_fetch_failed", sym=label or position))
        return True


def _finish_sell_pct_from_context(
    label: str, position: str, timeframe: str, canonical: str,
) -> bool:
    """Resolve the lot from stored symbol+timeframe. Never re-dispatch /sell {index} {pct}."""
    all_lots = list_active_positions()
    active = long_lots_for_sell(all_lots)
    query = str(label or "").strip() or str(position or "").strip()
    p = None
    if timeframe and query and not query.replace(".", "").isdigit():
        p = resolve_position_by_symbol_tf(active, query, timeframe)
    if not p:
        send_telegram_message(t("no_open_position", arg=str(query or position).upper()))
        return True
    return _continue_sell_after_position(p, {}, query, pct_token=canonical)


def _handle_sell_pos_callback(callback_query: dict) -> bool:
    from logger import log

    t0 = time.perf_counter()
    answer_callback_query(callback_query.get("id"))
    data = str(callback_query.get("data") or "")

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
        _log_callback_first_reply("sellpos", t0)
        return True

    parsed = parse_lot_callback(data, SELL_POS_CALLBACK_PREFIX)
    if parsed is None:
        send_telegram_message(_sell_menu_text("pct_expired"))
        _log_callback_first_reply("sellpos", t0)
        return True

    ticker, tf = parsed
    try:
        ok = _continue_sell_for_lot_identity(ticker, tf)
    except Exception as e:
        log(f"sellpos preview failed: {e}", "ERROR")
        send_telegram_message(t("price_fetch_failed", sym=ticker))
        ok = True
    _log_callback_first_reply("sellpos", t0)
    return ok


def _continue_sell_for_lot_identity(
    ticker: str, timeframe: str, pct_token: str | None = None,
) -> bool:
    """Start or finish the sell wizard for a lot identified by symbol+timeframe."""
    all_lots = list_active_positions()
    active = long_lots_for_sell(all_lots)
    p = resolve_position_by_symbol_tf(active, ticker, timeframe)
    if not p:
        send_telegram_message(t("no_open_position", arg=ticker.upper()))
        return True
    prices = {} if pct_token else _display_prices_for_lots([p])
    return _continue_sell_after_position(p, prices, ticker, pct_token=pct_token)


def _handle_lot_sell_callback(callback_query: dict) -> bool:
    """Stateless Verkaufen tap from /positions or a stop/fill (#455)."""
    chat_id = _buy_callback_chat_id(callback_query, "lotsell")
    if not chat_id:
        return True
    data = str(callback_query.get("data") or "")
    parsed = parse_lot_callback(data, LOT_SELL_CALLBACK_PREFIX)
    if parsed is None:
        send_telegram_message(_sell_menu_text("pct_expired"))
        return True
    ticker, tf = parsed
    return _continue_sell_for_lot_identity(ticker, tf)


def _handle_lot_why_callback(callback_query: dict) -> bool:
    """Warum? tap — reuse /why SYMBOL (timeframe is identity only)."""
    chat_id = _buy_callback_chat_id(callback_query, "poswhy")
    if not chat_id:
        return True
    data = str(callback_query.get("data") or "")
    parsed = parse_lot_callback(data, LOT_WHY_CALLBACK_PREFIX)
    if parsed is None:
        send_telegram_message(t("why_usage"))
        return True
    ticker, _tf = parsed
    from notifications.telegram_commands.router import dispatch_command

    return dispatch_command(f"/why {ticker}")


def _buy_callback_chat_id(callback_query: dict, log_label: str):
    from logger import log

    answer_callback_query(callback_query.get("id"))
    message = callback_query.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    if not chat_id:
        log(f"{log_label} callback missing chat id", "WARNING")
        return None
    set_chat_id(chat_id)
    return chat_id


def _handle_buy_coin_callback(callback_query: dict) -> bool:
    chat_id = _buy_callback_chat_id(callback_query, "buycoin")
    if not chat_id:
        return True
    data = str(callback_query.get("data") or "")
    raw = data.split(":", 1)[1] if ":" in data else ""
    entry = get_context(chat_id)
    meta = (entry or {}).get("meta") or {}
    if (
        not entry
        or entry.get("command") != "buy"
        or str(meta.get("state") or "") != "buy_awaiting_coin"
    ):
        send_telegram_message(_buy_menu_text("usdt_expired"))
        return True

    idx = safe_int(raw)
    if idx is None or idx < 1:
        send_telegram_message(_buy_menu_text("usdt_expired"))
        return True

    coins = list_coins()
    coin = resolve_coin_by_display_index(coins, idx - 1)
    if not coin:
        send_telegram_message(t("invalid_number_buy"))
        return True
    return _continue_buy_after_coin(_coin_symbol(coin), str(idx))


def _handle_buy_amt_callback(callback_query: dict) -> bool:
    chat_id = _buy_callback_chat_id(callback_query, "buyamt")
    if not chat_id:
        return True
    data = str(callback_query.get("data") or "")
    raw = data.split(":", 1)[1] if ":" in data else ""
    entry = get_context(chat_id)
    meta = (entry or {}).get("meta") or {}
    if not entry or entry.get("command") != "buy" or str(meta.get("state") or "") != "buy_awaiting_usdt":
        send_telegram_message(_buy_menu_text("usdt_expired"))
        return True

    coin = str(meta.get("coin") or "").strip()
    label = str(meta.get("label") or coin)
    usdt = safe_float(raw)
    if not coin or usdt is None or usdt <= 0:
        prompt_buy_amount(label, invalid=True)
        set_context(chat_id, "buy", **meta)
        return True

    from notifications.telegram_commands.router import dispatch_command

    clear_context(chat_id)
    return dispatch_command(f"/buy {coin} {raw}")


def _handle_buy_back_callback(callback_query: dict) -> bool:
    chat_id = _buy_callback_chat_id(callback_query, "buyback")
    if not chat_id:
        return True
    entry = get_context(chat_id)
    meta = (entry or {}).get("meta") or {}
    if (
        not entry
        or entry.get("command") != "buy"
        or str(meta.get("state") or "") != "buy_awaiting_usdt"
    ):
        send_telegram_message(_buy_menu_text("usdt_expired"))
        return True
    from notifications.telegram_commands.router import dispatch_command

    clear_context(chat_id)
    return dispatch_command("/buy")


def handle_callback(callback_query: dict) -> bool:
    data = str(callback_query.get("data") or "")
    if data.startswith(SELL_POS_CALLBACK_PREFIX):
        return _handle_sell_pos_callback(callback_query)
    if data.startswith(SELL_PCT_CALLBACK_PREFIX):
        return _handle_sell_pct_callback(callback_query)
    if data.startswith(LOT_SELL_CALLBACK_PREFIX):
        return _handle_lot_sell_callback(callback_query)
    if data.startswith(LOT_WHY_CALLBACK_PREFIX):
        return _handle_lot_why_callback(callback_query)
    if data.startswith(BUY_COIN_CALLBACK_PREFIX):
        return _handle_buy_coin_callback(callback_query)
    if data.startswith(BUY_AMT_CALLBACK_PREFIX):
        return _handle_buy_amt_callback(callback_query)
    if data == BUY_BACK_CALLBACK:
        return _handle_buy_back_callback(callback_query)

    from notifications.telegram_commands.manual_order_flow import handle_callback as handle_manual_callback

    return handle_manual_callback(callback_query)
