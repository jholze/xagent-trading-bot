"""Telegram /short and /cover — paper isolated shorts (v0)."""

from __future__ import annotations

from core.config import get_bot_config
from notifications.telegram_commands.command_context import activate_command
from notifications.telegram_commands.position_display import (
    position_symbol,
    resolve_position_by_symbol,
)
from notifications.telegram_commands.usage_hints import hint
from price_fetcher import get_prices, get_prices_batch
from services.trading_service import TradingService
from strategies.positions import get_position, list_active_positions
from strategies.short_math import is_short, snapshot
from strategies.short_policy import shorts_enabled
from telegram_notifier import send_telegram_message

_trading = TradingService()


def handle(text: str) -> bool:
    lower = (text or "").strip()
    if lower == "/short" or lower.startswith("/short "):
        return _handle_short(lower)
    if lower == "/cover" or lower.startswith("/cover "):
        return _handle_cover(lower)
    return False


def _handle_short(text: str) -> bool:
    activate_command("short")
    cfg = get_bot_config()
    if not shorts_enabled(cfg.raw):
        send_telegram_message("⚠️ Shorts aus (<code>shorts.enabled=false</code>).")
        return True
    parts = [p for p in text.split() if p.strip()]
    if len(parts) < 2:
        send_telegram_message(
            "Short (Paper): <code>/short H</code> · "
            "<code>/short H 400 2</code> (USDT, Hebel)"
        )
        return True
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
    if len(parts) >= 4:
        try:
            lev = float(parts[3])
        except ValueError:
            lev = None
    px = float((get_prices([sym]) or {}).get(sym) or 0)
    if px <= 0:
        send_telegram_message(f"Kein Preis für <code>{sym}</code>.")
        return True
    tf = "4h"
    result = _trading.refresh().execute_short(
        sym, tf, px, usdt=usdt, leverage=lev, source="manual"
    )
    if result.executed:
        pos = get_position(sym, tf)
        snap = snapshot(pos, px)
        send_telegram_message(
            f"🔻 <b>SHORT</b> <code>{sym}</code> {tf}\n"
            f"qty={result.amount:.6g} @ {px:g}  lev={snap.get('leverage')}×\n"
            f"margin≈{float(snap.get('margin') or 0):.0f}  liq≈{float(snap.get('liq_price') or 0):g}\n"
            f"{result.message or 'ok'}"
        )
    else:
        send_telegram_message(f"SHORT blockiert: {result.message}")
    return True


def _handle_cover(text: str) -> bool:
    activate_command("cover")
    parts = [p for p in text.split() if p.strip()]
    if len(parts) < 2:
        send_telegram_message("Cover: <code>/cover H</code> · <code>/cover H 50</code> (% )")
        return True
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
    pct = 100.0
    if len(parts) >= 3:
        try:
            pct = float(parts[2])
        except ValueError:
            pct = 100.0
    qty = float(pos.get("amount") or 0) * max(0.0, min(100.0, pct)) / 100.0
    px = float((prices or {}).get(sym) or 0)
    if px <= 0:
        px = float((get_prices([sym]) or {}).get(sym) or 0)
    result = _trading.refresh().execute_cover(sym, tf, px, amount=qty, source="manual")
    if result.executed:
        send_telegram_message(
            f"🔺 <b>COVER</b> <code>{sym}</code>\n"
            f"qty={result.amount:.6g} @ {px:g}  pnl={result.pnl:+.2f}\n"
            f"{result.message or 'ok'}"
        )
    else:
        send_telegram_message(f"COVER blockiert: {result.message}")
    return True
