"""Telegram /short and /cover — paper isolated shorts (v0).

Both commands only *request* a confirmation (#452): the typed line is parsed,
a risk preview with the resolved size/percent is sent, and ``execute_short`` /
``execute_cover`` run solely from the ``manual_ok`` callback in
``manual_order_flow`` — the same two-step shape as ``/buy`` and ``/sell``.
"""

from __future__ import annotations

from core.config import get_bot_config
from notifications.telegram_commands.command_context import activate_command, clear_context
from notifications.telegram_commands.manual_order_flow import (
    request_cover_confirmation,
    request_short_confirmation,
)
from notifications.telegram_commands.position_display import (
    position_symbol,
    resolve_position_by_symbol,
)
from price_fetcher import get_prices_batch
from services.trading_service import TradingService
from strategies.positions import get_position, list_active_positions
from strategies.short_math import is_short
from strategies.short_policy import shorts_enabled
from telegram_notifier import send_telegram_message

_trading = TradingService()

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
        activate_command("short")
        send_telegram_message(_SHORT_USAGE)
        return True
    clear_context()
    sym = parts[1].upper()
    if "/" not in sym:
        sym = f"{sym}/USDT"
    usdt = None
    lev = None
    if len(parts) >= 3:
        try:
            usdt = float(parts[2])
        except ValueError:
            send_telegram_message("USDT muss eine Zahl sein.")
            return True
        if usdt <= 0:
            send_telegram_message("USDT muss größer als 0 sein.")
            return True
    if len(parts) >= 4:
        try:
            lev = float(parts[3])
        except ValueError:
            send_telegram_message("Hebel muss eine Zahl sein (z.B. <code>2</code>).")
            return True
        if lev <= 0:
            send_telegram_message("Hebel muss größer als 0 sein.")
            return True
    px = float((get_prices_batch([sym]) or {}).get(sym) or 0)
    if px <= 0:
        send_telegram_message(f"Kein Preis für <code>{sym}</code>.")
        return True
    tf = "4h"
    return request_short_confirmation(
        _trading, symbol=sym, timeframe=tf, price=px, usdt=usdt, leverage=lev,
    )


def _handle_cover(text: str) -> bool:
    parts = [p for p in text.split() if p.strip()]
    if len(parts) < 2:
        activate_command("cover")
        send_telegram_message(_COVER_USAGE)
        return True
    clear_context()
    q = parts[1]
    active = list_active_positions()
    prices = get_prices_batch([position_symbol(p) for p in active] or [q])
    p = resolve_position_by_symbol(active, q, prices)
    if not p:
        send_telegram_message(f"Keine Position für <code>{q}</code>.")
        return True
    sym = position_symbol(p)
    tf = p.get("timeframe") or "4h"
    pos = get_position(sym, tf)
    if not is_short(pos):
        send_telegram_message(f"<code>{sym}</code> ist kein Short — nutze /sell.")
        return True
    # Fraction of the short to cover; the bare form is an explicit 100 % that
    # the preview spells out — never a silent default.
    pct = 1.0
    if len(parts) >= 3:
        try:
            pct = float(parts[2].rstrip("%")) / 100.0
        except ValueError:
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
    px = float((prices or {}).get(sym) or 0)
    if px <= 0:
        px = float((get_prices_batch([sym]) or {}).get(sym) or 0)
    if px <= 0:
        send_telegram_message(f"Kein Preis für <code>{sym}</code>.")
        return True
    return request_cover_confirmation(
        _trading, symbol=sym, timeframe=tf, price=px, amount=qty, pct=pct,
    )
