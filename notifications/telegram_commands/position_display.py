from core.config import get_bot_config


def position_symbol(p: dict) -> str:
    sym = p["symbol"]
    return sym if "/" in sym else f"{sym}/USDT"


def _pnl_emoji(value: float) -> str:
    if value > 0.05:
        return "🟢"
    if value < -0.05:
        return "🔴"
    return "🟡"


def _fmt_pct(value: float) -> str:
    return f"{value:+.1f}%"


def sort_positions_by_value(active: list, prices: dict) -> list:
    """Return positions sorted by current USDT value (highest first).

    Display (/sell, /positions) and sell execution must use the same order.
    """
    enriched = []
    for p in active:
        sym = position_symbol(p)
        price = float(prices.get(sym, 0) or 0)
        m = _position_metrics(p, price)
        enriched.append((p, m["value_usdt"]))
    enriched.sort(key=lambda row: row[1], reverse=True)
    return [p for p, _ in enriched]


def resolve_position_by_display_index(active: list, prices: dict, index: int):
    """Map 0-based display index (from numbered /sell list) to a position dict."""
    sorted_active = sort_positions_by_value(active, prices)
    if 0 <= index < len(sorted_active):
        return sorted_active[index]
    return None


def _position_metrics(p: dict, price: float) -> dict:
    entry = float(p.get("average_entry", p.get("entry_price", 0)) or 0)
    amount = float(p.get("amount", 0))
    value_usdt = price * amount if price > 0 else 0.0
    unreal = (price - entry) * amount if entry > 0 and price > 0 else 0.0
    unreal_pct = ((price / entry) - 1) * 100 if entry > 0 and price > 0 else 0.0
    sold_pct = min(float(p.get("sold_percent", 0) or 0), 1.0) * 100
    sold_raw = float(p.get("sold_percent", 0) or 0)
    return {
        "entry": entry,
        "amount": amount,
        "price": price,
        "value_usdt": value_usdt,
        "unreal": unreal,
        "unreal_pct": unreal_pct,
        "sold_pct": sold_pct,
        "sold_warn": sold_raw > 1.0,
    }


def format_position_card(index: int, p: dict, price: float, numbered: bool = False) -> str:
    sym = position_symbol(p)
    m = _position_metrics(p, price)
    prefix = f"<b>{index}.</b> " if numbered else ""
    ticker = sym.split("/")[0]
    pnl_icon = _pnl_emoji(m["unreal"])

    sold_line = ""
    if m["sold_pct"] > 0 or m["sold_warn"]:
        sold_raw_pct = float(p.get("sold_percent", 0) or 0) * 100
        sold_val = f"{m['sold_pct']:.0f}%" if not m["sold_warn"] else f"⚠️ {sold_raw_pct:.0f}%"
        sold_line = f"\n   └ Bereits verkauft: <b>{sold_val}</b>"

    last = p.get("last_action")
    last_line = f" · Letzte Aktion: <b>{last}</b>" if last else ""

    price_str = f"${m['price']:.4f}" if m["price"] > 0 else "—"
    entry_str = f"${m['entry']:.4f}" if m["entry"] > 0 else "—"

    return (
        f"{prefix}<b>{ticker}</b> {pnl_icon} <code>{_fmt_pct(m['unreal_pct'])}</code>\n"
        f"   └ <code>{m['amount']:.4f}</code> @ {price_str} · Entry {entry_str}\n"
        f"   └ Wert <b>${m['value_usdt']:.1f}</b> · PnL <b>${m['unreal']:+.1f}</b>"
        f"{sold_line}{last_line}"
    )


def format_portfolio_summary(history: dict, total_unreal: float, position_count: int, mode_label: str = "") -> str:
    balance = float(history.get("virtual_balance", 0))
    realized = float(history.get("realized_pnl", 0))
    total_value = balance + total_unreal
    total_pnl = realized + total_unreal
    initial = float(get_bot_config().initial_capital_usdt or 5000)
    pnl_pct = (total_pnl / initial * 100) if initial > 0 else 0.0
    pnl_icon = _pnl_emoji(total_pnl)

    mode_line = f" · <i>{mode_label}</i>" if mode_label else ""
    return (
        f"<b>📊 Portfolio</b>{mode_line}\n\n"
        f"💵 Cash <b>${balance:,.0f}</b>\n"
        f"📈 Unrealisiert <b>${total_unreal:+.1f}</b>\n"
        f"💰 Gesamtwert <b>${total_value:,.0f}</b>\n"
        f"{pnl_icon} Gesamt-PnL <b>${total_pnl:+.1f}</b> (<code>{pnl_pct:+.1f}%</code>)\n"
        f"✅ Realisiert <b>${realized:+.1f}</b>\n\n"
        f"<b>Positionen ({position_count})</b>"
    )


def format_positions_message(
    active: list,
    prices: dict,
    history: dict,
    mode_label: str = "",
    include_trades: bool = True,
    numbered: bool = False,
    title: str = None,
) -> str:
    if not active:
        empty = (
            "<b>📊 Portfolio</b>\n\n"
            "Keine offenen Positionen.\n"
            f"💵 Cash <b>${float(history.get('virtual_balance', 0)):,.0f}</b>"
        )
        if mode_label:
            empty += f"\n<i>{mode_label}</i>"
        if include_trades:
            empty += "\n\n<b>Letzte Trades</b>\n"
            trades = history.get("trades", [])[-5:]
            if not trades:
                empty += "<i>Keine Trades im Ledger.</i>"
            else:
                for t in reversed(trades):
                    ts = t.get("timestamp", "")[:16].replace("T", " ")
                    typ = "🟢 Kauf" if t.get("type") == "BUY" else "🔴 Verkauf"
                    sym = (t.get("symbol") or "").replace("/USDT", "")
                    pnl = t.get("pnl")
                    pnl_part = f" · PnL <b>${pnl:+.1f}</b>" if pnl is not None else ""
                    empty += (
                        f"\n{typ} <b>{sym}</b> · {ts}\n"
                        f"   └ <code>{float(t.get('amount', 0)):.4f}</code> @ "
                        f"${float(t.get('price', 0)):.4f}{pnl_part}"
                    )
        return empty

    total_unreal = 0.0
    sorted_active = sort_positions_by_value(active, prices)
    rows = []
    for p in sorted_active:
        sym = position_symbol(p)
        price = float(prices.get(sym, 0) or 0)
        m = _position_metrics(p, price)
        total_unreal += m["unreal"]
        rows.append((p, price))

    if title:
        msg = f"<b>{title}</b>\n\n"
    else:
        msg = format_portfolio_summary(history, total_unreal, len(active), mode_label) + "\n"

    cards = []
    for i, (p, price) in enumerate(rows, 1):
        cards.append(format_position_card(i, p, price, numbered=numbered))
    msg += "\n\n".join(cards)

    if include_trades:
        msg += "\n\n<b>Letzte Trades</b>\n"
        trades = history.get("trades", [])[-5:]
        if not trades:
            msg += "<i>Keine Trades im Ledger.</i>"
        else:
            for t in reversed(trades):
                ts = t.get("timestamp", "")[:16].replace("T", " ")
                typ = "🟢 Kauf" if t.get("type") == "BUY" else "🔴 Verkauf"
                sym = (t.get("symbol") or "").replace("/USDT", "")
                pnl = t.get("pnl")
                pnl_part = f" · PnL <b>${pnl:+.1f}</b>" if pnl is not None else ""
                msg += (
                    f"\n{typ} <b>{sym}</b> · {ts}\n"
                    f"   └ <code>{float(t.get('amount', 0)):.4f}</code> @ "
                    f"${float(t.get('price', 0)):.4f}{pnl_part}"
                )

    return msg


def format_sell_list_message(active: list, prices: dict) -> str:
    msg = format_positions_message(
        active,
        prices,
        load_trade_history_safe(),
        include_trades=False,
        numbered=True,
        title="📍 Positionen verkaufen",
    )
    return msg + "\n\n<code>/sell NUMMER PROZENT</code>  ·  z.B. <code>/sell 1 30</code>"


def load_trade_history_safe() -> dict:
    from data_manager import load_trade_history
    return load_trade_history()


def format_trade_banner(result) -> str:
    sym = (result.symbol or "").replace("/USDT", "")
    price = float(result.price or 0)
    amount = float(result.amount or 0)
    usdt = float(result.usdt_amount or 0)
    if result.order_type == "BUY":
        return (
            f"✅ <b>Kauf ausgeführt</b> — <b>{sym}</b>\n"
            f"   └ <code>{amount:.4f}</code> @ ${price:.4f} · <b>${usdt:.0f}</b>"
        )
    pnl_part = f" · PnL <b>${result.pnl:+.1f}</b>" if result.pnl is not None else ""
    return (
        f"✅ <b>Verkauf ausgeführt</b> — <b>{sym}</b>\n"
        f"   └ <code>{amount:.4f}</code> @ ${price:.4f} · <b>${usdt:.0f}</b>{pnl_part}"
    )


def send_positions_snapshot(trade_result=None, mode_label: str = None) -> bool:
    """Send portfolio overview to Telegram; optional trade banner after buy/sell."""
    from data_manager import load_trade_history
    from price_fetcher import get_prices_batch
    from services.trading_service import TradingService
    from strategies.positions import list_active_positions
    from telegram_notifier import send_telegram_message

    active = list_active_positions()
    history = load_trade_history()
    symbols = [position_symbol(p) for p in active]
    prices = get_prices_batch(symbols) if symbols else {}
    mode = mode_label or TradingService().mode_label()
    msg = format_positions_message(
        active,
        prices,
        history,
        mode_label=mode,
        include_trades=True,
    )
    if trade_result is not None and getattr(trade_result, "executed", False):
        msg = f"{format_trade_banner(trade_result)}\n\n{msg}"
    return send_telegram_message(msg)