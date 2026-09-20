"""Telegram /short and /cover — paper isolated shorts (v0).

Both commands only *request* a confirmation (#452): the typed line is parsed,
a risk preview with the resolved size/percent is sent, and ``execute_short`` /
``execute_cover`` run solely from the ``manual_ok`` callback in
``manual_order_flow`` — the same two-step shape as ``/buy`` and ``/sell``.

#512 adds tap-first pickers (watchlist coin → size; open short lot → %).
Typed ``/short H`` and ``/cover H 50`` stay as power-user aliases.
"""

from __future__ import annotations

from core.config import get_bot_config
from data_manager import list_coins
from notifications.telegram_commands.command_context import (
    activate_command,
    clear_context,
    get_context,
    set_chat_id,
)
from notifications.telegram_commands.manual_order_flow import (
    request_cover_confirmation,
    request_short_confirmation,
)
from notifications.telegram_commands.position_display import (
    encode_lot_callback,
    lot_ticker,
    lot_timeframe,
    parse_lot_callback,
    position_symbol,
    resolve_position_by_symbol,
    resolve_position_by_symbol_tf,
)
from notifications.telegram_commands.utils import safe_float, safe_int
from notifications.telegram_commands.watchlist_commands import (
    _coin_symbol,
    resolve_coin_by_display_index,
)
from notifications.telegram_i18n import t
from price_fetcher import get_prices_batch
from services.trading_service import TradingService
from strategies.positions import get_position, list_active_positions
from strategies.short_math import is_short
from strategies.short_policy import shorts_enabled
from telegram_notifier import answer_callback_query, send_telegram_buttons, send_telegram_message

_trading = TradingService()

SHORT_COIN_CALLBACK_PREFIX = "shortcoin:"
SHORT_AMT_CALLBACK_PREFIX = "shortamt:"
SHORT_BACK_CALLBACK = "shortback"
COVER_LOT_CALLBACK_PREFIX = "coverlot:"
COVER_PCT_CALLBACK_PREFIX = "coverpct:"
COVER_BACK_CALLBACK = "coverback"
_COVER_PCT_PRESETS = (25, 50, 75, 100)

_SHORT_USAGE = (
    "Short (Paper): <code>/short H</code> · "
    "<code>/short H 400 2</code> (USDT, Hebel)\n"
    "Ohne Betrag/Hebel gilt die Standardgröße — sie wird vor der Ausführung angezeigt "
    "und muss bestätigt werden."
)
_COVER_USAGE = (
    "Cover: <code>/cover H</code> (100 %) · <code>/cover H 50</code> (% )\n"
    "Der Anteil wird vor der Ausführung angezeigt und muss bestätigt werden."
)


def _command_menu_text(command: str, field: str, **kwargs) -> str:
    from notifications.telegram_commands.menu_i18n import _command_entry, _pack, current_language

    template = str(_command_entry(_pack(current_language()), command).get(field) or "")
    for key, value in kwargs.items():
        template = template.replace(f"{{{key}}}", str(value))
    return template


def _short_menu_text(field: str, **kwargs) -> str:
    return _command_menu_text("short", field, **kwargs)


def _cover_menu_text(field: str, **kwargs) -> str:
    return _command_menu_text("cover", field, **kwargs)


def _short_usdt_presets(max_usdt: float) -> list[int]:
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


def format_short_pick_button_label(coin: dict, price: float) -> str:
    from price_fetcher import format_usdt_price

    ticker = _coin_symbol(coin).split("/")[0]
    if price and price > 0:
        return f"{ticker} {format_usdt_price(price)}"
    return ticker


def _short_coin_button_rows(coins: list, prices: dict) -> list:
    rows = []
    for index, coin in enumerate(coins, start=1):
        px = float(prices.get(_coin_symbol(coin), 0) or 0)
        rows.append([{
            "text": format_short_pick_button_label(coin, px),
            "callback_data": f"{SHORT_COIN_CALLBACK_PREFIX}{index}",
        }])
    return rows


def _open_shorts() -> list:
    return [p for p in (list_active_positions() or []) if is_short(p)]


def _cover_lot_button_label(p: dict) -> str:
    ticker = lot_ticker(position_symbol(p))
    tf = lot_timeframe(p.get("timeframe"))
    return f"{ticker} · {tf}"


def _cover_lot_button_rows(lots: list) -> list:
    rows = []
    for p in lots:
        rows.append([{
            "text": _cover_lot_button_label(p),
            "callback_data": encode_lot_callback(
                COVER_LOT_CALLBACK_PREFIX,
                position_symbol(p),
                p.get("timeframe"),
            ),
        }])
    return rows


def prompt_short_amount(coin_label: str, *, invalid: bool = False, max_usdt: float | None = None) -> None:
    """Ask for a short size. Presets include max_usdt_per_trade; nothing is assumed."""
    prompt = _short_menu_text("usdt_prompt", coin=coin_label)
    if invalid:
        prompt = _short_menu_text("usdt_invalid") + "\n\n" + prompt
    if max_usdt is None:
        max_usdt = get_bot_config().max_usdt_per_trade
    presets = _short_usdt_presets(max_usdt)
    buttons = [[
        {
            "text": _short_menu_text("usdt_btn", usdt=str(n)),
            "callback_data": f"{SHORT_AMT_CALLBACK_PREFIX}{n}",
        }
        for n in presets
    ]]
    buttons.append([{
        "text": _short_menu_text("back_btn"),
        "callback_data": SHORT_BACK_CALLBACK,
    }])
    send_telegram_buttons(prompt, buttons)


def prompt_cover_percentage(position_label: str, *, invalid: bool = False) -> None:
    prompt = _cover_menu_text("pct_prompt", position=position_label)
    if invalid:
        prompt = _cover_menu_text("pct_invalid") + "\n\n" + prompt
    buttons = [[
        {
            "text": _cover_menu_text(f"pct_btn_{n}"),
            "callback_data": f"{COVER_PCT_CALLBACK_PREFIX}{n}",
        }
        for n in _COVER_PCT_PRESETS
    ]]
    buttons.append([{
        "text": _cover_menu_text("back_btn"),
        "callback_data": COVER_BACK_CALLBACK,
    }])
    send_telegram_buttons(prompt, buttons)


def _show_short_coin_picker() -> bool:
    # Frozen #452: bare /short must still mention confirmation on send_telegram_message,
    # even if the watchlist fetch fails (unit tests block Gate).
    prompt = _short_menu_text("coin_prompt") or _SHORT_USAGE
    try:
        coins = list_coins()
    except Exception:
        coins = []
    if not coins:
        send_telegram_message(prompt)
        return True
    symbols = [_coin_symbol(c) for c in coins]
    try:
        prices = get_prices_batch(symbols) or {}
    except Exception:
        prices = {}
    buttons = _short_coin_button_rows(coins, prices)
    send_telegram_message(prompt, reply_markup={"inline_keyboard": buttons})
    activate_command("short", state="short_awaiting_coin")
    return True


def _show_cover_lot_picker() -> bool:
    shorts = _open_shorts()
    if not shorts:
        send_telegram_message(_cover_menu_text("empty") or _COVER_USAGE)
        return True
    prompt = _cover_menu_text("lot_prompt") or _COVER_USAGE
    send_telegram_buttons(prompt, _cover_lot_button_rows(shorts))
    activate_command("cover", state="cover_awaiting_lot")
    return True


def _continue_short_after_coin(sym: str, arg: str, usdt_token: str | None = None) -> bool:
    if usdt_token is None:
        ticker = sym.split("/")[0]
        pos_token = arg if str(arg).replace(".", "").isdigit() else str(arg).upper()
        prompt_short_amount(ticker)
        activate_command(
            "short",
            state="short_awaiting_usdt",
            coin=pos_token,
            label=ticker,
        )
        return True
    return _request_short(sym, usdt_token=usdt_token, lev_token=None)


def _continue_cover_after_lot(p: dict, pct_token: str | None = None) -> bool:
    """Shared follow-up after a short lot is resolved (tap or typed symbol+tf)."""
    sym = position_symbol(p)
    tf = p.get("timeframe") or "4h"
    ticker = lot_ticker(sym)
    pos_token = f"{ticker}:{lot_timeframe(tf)}"
    if pct_token is None:
        prompt_cover_percentage(f"{ticker} · {lot_timeframe(tf)}")
        activate_command(
            "cover",
            state="cover_awaiting_pct",
            position=pos_token,
            symbol=sym,
            timeframe=str(tf),
            label=ticker,
        )
        return True
    return _request_cover_lot(p, pct_token)


def _request_short(sym: str, usdt_token: str | None = None, lev_token: str | None = None) -> bool:
    usdt = None
    lev = None
    if usdt_token is not None:
        try:
            usdt = float(usdt_token)
        except (TypeError, ValueError):
            send_telegram_message("USDT muss eine Zahl sein.")
            return True
        if usdt <= 0:
            send_telegram_message("USDT muss größer als 0 sein.")
            return True
    if lev_token is not None:
        try:
            lev = float(lev_token)
        except (TypeError, ValueError):
            send_telegram_message("Hebel muss eine Zahl sein (z.B. <code>2</code>).")
            return True
        if lev <= 0:
            send_telegram_message("Hebel muss größer als 0 sein.")
            return True
    px = float((get_prices_batch([sym]) or {}).get(sym) or 0)
    if px <= 0:
        send_telegram_message(f"Kein Preis für <code>{sym}</code>.")
        return True
    return request_short_confirmation(
        _trading, symbol=sym, timeframe="4h", price=px, usdt=usdt, leverage=lev,
    )


def _request_cover_lot(p: dict, pct_token: str | None = None) -> bool:
    sym = position_symbol(p)
    tf = p.get("timeframe") or "4h"
    pos = get_position(sym, tf)
    if not is_short(pos):
        send_telegram_message(f"<code>{sym}</code> ist kein Short — nutze /sell.")
        return True
    pct = 1.0
    if pct_token is not None:
        try:
            pct = float(str(pct_token).rstrip("%")) / 100.0
        except (TypeError, ValueError):
            send_telegram_message("Prozent muss eine Zahl sein (z.B. <code>/cover H 50</code>).")
            return True
        if pct <= 0 or pct > 1.0:
            send_telegram_message("Prozent muss zwischen 1 und 100 liegen.")
            return True
    total = float(pos.get("amount") or 0)
    qty = total * pct
    if qty <= 0:
        send_telegram_message(f"Keine deckbare Menge für <code>{sym}</code>.")
        return True
    px = float((get_prices_batch([sym]) or {}).get(sym) or 0)
    if px <= 0:
        send_telegram_message(f"Kein Preis für <code>{sym}</code>.")
        return True
    return request_cover_confirmation(
        _trading, symbol=sym, timeframe=tf, price=px, amount=qty, pct=pct,
    )


def handle(text: str) -> bool:
    lower = (text or "").strip()
    if lower == "/short" or lower.startswith("/short "):
        return _handle_short(lower)
    if lower == "/cover" or lower.startswith("/cover "):
        return _handle_cover(lower)
    return False


def _handle_short(text: str) -> bool:
    cfg = get_bot_config()
    if not shorts_enabled(cfg.raw):
        send_telegram_message("⚠️ Shorts aus (<code>shorts.enabled=false</code>).")
        return True
    parts = [p for p in text.split() if p.strip()]
    if len(parts) < 2:
        return _show_short_coin_picker()
    clear_context()
    raw = parts[1]
    if str(raw).replace(".", "").isdigit():
        idx = safe_int(raw)
        coin = resolve_coin_by_display_index(list_coins(), (idx or 0) - 1)
        if not coin:
            send_telegram_message(t("invalid_number_buy"))
            return True
        sym = _coin_symbol(coin)
    else:
        sym = raw.upper()
        if "/" not in sym:
            sym = f"{sym}/USDT"
    usdt_token = parts[2] if len(parts) >= 3 else None
    lev_token = parts[3] if len(parts) >= 4 else None
    if usdt_token is None and lev_token is None and len(parts) == 2:
        # Typed /short H — power-user alias: existing confirm with defaults.
        return _request_short(sym, usdt_token=None, lev_token=None)
    return _request_short(sym, usdt_token=usdt_token, lev_token=lev_token)


def _parse_cover_query(q: str, active: list, prices: dict):
    raw = (q or "").strip()
    if ":" in raw and "/" not in raw:
        ticker, _, tf_raw = raw.partition(":")
        if ticker and tf_raw:
            return resolve_position_by_symbol_tf(active, ticker, tf_raw)
    return resolve_position_by_symbol(active, raw, prices)


def _handle_cover(text: str) -> bool:
    parts = [p for p in text.split() if p.strip()]
    if len(parts) < 2:
        return _show_cover_lot_picker()
    clear_context()
    q = parts[1]
    active = list_active_positions()
    prices = get_prices_batch([position_symbol(p) for p in active] or [q])
    p = _parse_cover_query(q, active, prices)
    if not p:
        send_telegram_message(f"Keine Position für <code>{q}</code>.")
        return True
    pct_token = parts[2] if len(parts) >= 3 else None
    return _request_cover_lot(p, pct_token)


def _callback_chat_id(callback_query: dict, log_label: str):
    from logger import log

    callback_id = (callback_query or {}).get("id")
    if callback_id:
        answer_callback_query(callback_id)
    chat_id = ((callback_query or {}).get("message") or {}).get("chat") or {}
    chat_id = chat_id.get("id") if isinstance(chat_id, dict) else None
    if not chat_id:
        log(f"{log_label} callback missing chat id", "WARNING")
        return None
    set_chat_id(chat_id)
    return chat_id


def _wizard_entry(chat_id, command: str, state: str) -> dict | None:
    entry = get_context(chat_id)
    meta = (entry or {}).get("meta") or {}
    if not entry or entry.get("command") != command or str(meta.get("state") or "") != state:
        return None
    return entry


def _handle_short_coin_callback(callback_query: dict) -> bool:
    chat_id = _callback_chat_id(callback_query, "shortcoin")
    if not chat_id:
        return True
    if _wizard_entry(chat_id, "short", "short_awaiting_coin") is None:
        send_telegram_message(_short_menu_text("usdt_expired"))
        return True
    data = str((callback_query or {}).get("data") or "")
    raw = data.split(":", 1)[1] if ":" in data else ""
    idx = safe_int(raw)
    if idx is None or idx < 1:
        send_telegram_message(_short_menu_text("usdt_expired"))
        return True
    coins = list_coins()
    coin = resolve_coin_by_display_index(coins, idx - 1)
    if not coin:
        send_telegram_message(t("watchlist_empty"))
        return True
    return _continue_short_after_coin(_coin_symbol(coin), str(idx))


def _handle_short_amt_callback(callback_query: dict) -> bool:
    chat_id = _callback_chat_id(callback_query, "shortamt")
    if not chat_id:
        return True
    entry = _wizard_entry(chat_id, "short", "short_awaiting_usdt")
    if entry is None:
        send_telegram_message(_short_menu_text("usdt_expired"))
        return True
    data = str((callback_query or {}).get("data") or "")
    raw = data.split(":", 1)[1] if ":" in data else ""
    meta = entry.get("meta") or {}
    coin = str(meta.get("coin") or "").strip()
    label = str(meta.get("label") or coin)
    usdt = safe_float(raw)
    if not coin or usdt is None or usdt <= 0:
        prompt_short_amount(label, invalid=True)
        return True
    from notifications.telegram_commands.router import dispatch_command

    clear_context(chat_id)
    return dispatch_command(f"/short {coin} {raw}")


def _handle_short_back_callback(callback_query: dict) -> bool:
    chat_id = _callback_chat_id(callback_query, "shortback")
    if not chat_id:
        return True
    entry = _wizard_entry(chat_id, "short", "short_awaiting_usdt")
    if entry is None:
        send_telegram_message(_short_menu_text("usdt_expired"))
        return True
    clear_context(chat_id)
    return _show_short_coin_picker()


def _handle_cover_lot_callback(callback_query: dict) -> bool:
    chat_id = _callback_chat_id(callback_query, "coverlot")
    if not chat_id:
        return True
    if _wizard_entry(chat_id, "cover", "cover_awaiting_lot") is None:
        send_telegram_message(_cover_menu_text("pct_expired"))
        return True
    data = str((callback_query or {}).get("data") or "")
    parsed = parse_lot_callback(data, COVER_LOT_CALLBACK_PREFIX)
    if parsed is None:
        send_telegram_message(_cover_menu_text("pct_expired"))
        return True
    ticker, tf = parsed
    p = resolve_position_by_symbol_tf(_open_shorts(), ticker, tf)
    if not p:
        send_telegram_message(_cover_menu_text("pct_expired"))
        return True
    return _continue_cover_after_lot(p)


def _handle_cover_pct_callback(callback_query: dict) -> bool:
    chat_id = _callback_chat_id(callback_query, "coverpct")
    if not chat_id:
        return True
    entry = _wizard_entry(chat_id, "cover", "cover_awaiting_pct")
    if entry is None:
        send_telegram_message(_cover_menu_text("pct_expired"))
        return True
    data = str((callback_query or {}).get("data") or "")
    raw = data.split(":", 1)[1] if ":" in data else ""
    meta = entry.get("meta") or {}
    label = str(meta.get("label") or meta.get("position") or "")
    pct = safe_float(raw)
    if pct is None or pct <= 0 or pct > 100:
        prompt_cover_percentage(label, invalid=True)
        return True
    position = str(meta.get("position") or "").strip()
    if not position:
        send_telegram_message(_cover_menu_text("pct_expired"))
        return True
    from notifications.telegram_commands.router import dispatch_command

    clear_context(chat_id)
    return dispatch_command(f"/cover {position} {raw}")


def _handle_cover_back_callback(callback_query: dict) -> bool:
    chat_id = _callback_chat_id(callback_query, "coverback")
    if not chat_id:
        return True
    entry = _wizard_entry(chat_id, "cover", "cover_awaiting_pct")
    if entry is None:
        send_telegram_message(_cover_menu_text("pct_expired"))
        return True
    clear_context(chat_id)
    return _show_cover_lot_picker()


def handle_callback(callback_query: dict) -> bool:
    data = str((callback_query or {}).get("data") or "")
    if data.startswith(SHORT_COIN_CALLBACK_PREFIX):
        return _handle_short_coin_callback(callback_query)
    if data.startswith(SHORT_AMT_CALLBACK_PREFIX):
        return _handle_short_amt_callback(callback_query)
    if data == SHORT_BACK_CALLBACK:
        return _handle_short_back_callback(callback_query)
    if data.startswith(COVER_LOT_CALLBACK_PREFIX):
        return _handle_cover_lot_callback(callback_query)
    if data.startswith(COVER_PCT_CALLBACK_PREFIX):
        return _handle_cover_pct_callback(callback_query)
    if data == COVER_BACK_CALLBACK:
        return _handle_cover_back_callback(callback_query)
    return False
