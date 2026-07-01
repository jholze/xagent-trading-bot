"""Rich /orders detail view with position trail and PnL summary."""

from __future__ import annotations

from notifications.telegram_commands.position_ledger import (
    _branch_char,
    cycle_for_display_seq,
    cycle_realized_usd,
    cycle_unreal_usd,
    events_for_cycle,
    format_event_line,
    format_gesamt_line,
    orders_for_position,
    replay_position_events,
)
from services.order_service import (
    _fmt_price,
    _format_ts_short,
    _trade_date_label,
    ledger_label,
    source_label,
)


def _status_icon(status: str) -> str:
    icons = {
        "filled": "✅",
        "rejected": "❌",
        "cancelled": "🚫",
        "pending_confirmation": "⏳",
        "failed": "⚠️",
        "expired": "⌛",
        "executing": "🔄",
    }
    return icons.get(status or "", "·")


def _trail_line(ev: dict, *, selected_seq: int | None) -> str:
    seq = ev.get("display_seq")
    seq_part = f"<b>#{seq}</b> · " if seq else ""
    body = format_event_line(ev)
    prefix = "▶ " if selected_seq and seq == selected_seq else ""
    return f"{prefix}{seq_part}{body}"


def _selected_order_block(order: dict) -> list[str]:
    from notifications.coin_links import format_ticker_html

    sym = (order.get("symbol") or "").replace("/USDT", "")
    sym_html = format_ticker_html(sym, symbol_suffix="")
    side = (order.get("side") or "?").upper()
    status = (order.get("status") or "").upper()
    seq = order.get("display_seq", "?")
    tf = order.get("timeframe") or "4h"
    signal = (order.get("signal") or "").strip()
    req = order.get("request", {})
    exe = order.get("execution", {})
    risk = order.get("risk", {})
    ts = order.get("timestamps", {})

    lines = [
        f"<b>📋 Order #{seq}</b>  {_status_icon(order.get('status'))} {status}",
        f"{side} <b>{sym_html}</b> · {source_label(order.get('source', 'auto'))} · "
        f"{ledger_label(order.get('ledger_scope'))} · <code>{tf}</code>",
    ]
    if signal:
        lines.append(f"Signal <code>{signal}</code>")

    usdt = float(exe.get("usdt") or req.get("usdt") or 0)
    price = float(exe.get("price") or req.get("price") or 0)
    amount = float(exe.get("amount") or req.get("amount") or 0)
    if exe:
        lines.append(
            f"Ausführung · <code>{amount:.4f}</code> @ {_fmt_price(price)} · "
            f"<b>${usdt:,.0f}</b>"
        )
        if exe.get("fee"):
            lines.append(f"   Fee <b>${float(exe['fee']):.4f}</b>")
    elif req:
        if req.get("usdt"):
            lines.append(f"Anfrage · <b>${float(req['usdt']):,.0f}</b> @ {_fmt_price(price)}")
        if req.get("amount"):
            lines.append(f"   Menge <code>{float(req['amount']):.4f}</code>")
        if req.get("pct"):
            lines.append(f"   Anteil <b>{float(req['pct']) * 100:.0f}%</b>")

    if order.get("pnl") is not None:
        lines.append(f"Trade-PnL <b>${float(order['pnl']):+.2f}</b>")

    if risk.get("message"):
        lines.append(f"Risk · <i>{risk['message']}</i>")

    trade_ts = _format_ts_short(ts.get("filled") or ts.get("created") or "")
    if trade_ts:
        lines.append(f"{_trade_date_label(order.get('side'))} · {trade_ts}")

    if order.get("error"):
        lines.append(f"Fehler · {order['error']}")

    return lines


def _position_trail_block(
    order: dict,
    *,
    scope: str,
    selected_seq: int,
    max_events: int = 12,
) -> list[str]:
    symbol = order.get("symbol", "")
    tf = order.get("timeframe") or "4h"
    if not symbol:
        return []

    filled = orders_for_position(symbol, tf, scope)
    if not filled:
        return ["<i>Keine ausgeführten Trades für diese Position.</i>"]

    mark = 0.0
    amount = 0.0
    entry = 0.0
    try:
        from strategies.positions import get_position

        pos = get_position(symbol, tf)
        amount = float(pos.get("amount", 0) or 0)
        entry = float(pos.get("average_entry", pos.get("entry_price", 0)) or 0)
        if amount > 0:
            from price_fetcher import get_prices_batch

            sym_key = symbol if "/" in symbol else f"{symbol}/USDT"
            prices, _ = get_prices_batch([sym_key], fallbacks={})
            mark = float(prices.get(sym_key, 0) or 0)
    except Exception:
        pass

    all_events = replay_position_events(filled, mark_price=mark)
    view_cycle = cycle_for_display_seq(all_events, selected_seq)
    events = events_for_cycle(all_events, view_cycle)
    current_cycle = int(all_events[-1].get("cycle", 1)) if all_events else 1
    prior_cycles = view_cycle - 1
    is_current_cycle = view_cycle == current_cycle

    realized = cycle_realized_usd(events)
    if is_current_cycle and amount > 0 and entry > 0 and mark > 0:
        unreal = (mark - entry) * amount
        value_usdt = mark * amount
    else:
        unreal = cycle_unreal_usd(events)
        value_usdt = sum(
            float(ev.get("open_qty", 0) or 0) * mark
            for ev in events
            if ev.get("kind") == "entry" and float(ev.get("open_qty", 0) or 0) > 0
        )

    hidden = max(0, len(events) - max_events)
    visible = events[-max_events:] if hidden else events

    ticker = symbol.replace("/USDT", "")
    lines = [
        "",
        f"<b>📊 Position-Trail — {ticker} {tf}</b>",
    ]
    if view_cycle > 1:
        lines.append(f"<i>Zyklus {view_cycle}</i>")

    extra = (1 if prior_cycles else 0) + (1 if hidden else 0)
    total_branches = 1 + len(visible) + extra
    idx = 0
    lines.append(
        f"   {_branch_char(idx, total_branches)} {format_gesamt_line(
            value_usdt=value_usdt, unreal_usd=unreal, realized_usd=realized,
        )}"
    )
    idx += 1

    if prior_cycles:
        lines.append(
            f"   {_branch_char(idx, total_branches)} "
            f"<i>··· {prior_cycles} frühere Zyklen ausgeblendet ···</i>"
        )
        idx += 1

    for ev in visible:
        branch = _branch_char(idx, total_branches)
        idx += 1
        lines.append(f"   {branch} {_trail_line(ev, selected_seq=selected_seq)}")

    if hidden:
        lines.append(f"   └─ … +{hidden} ältere Transaktionen im Zyklus")

    total_pnl = unreal + realized
    lines.extend([
        "",
        f"Σ Zyklus {view_cycle} · Real <b>${realized:+.0f}</b> · Unreal <b>${unreal:+.0f}</b> · "
        f"Gesamt <b>${total_pnl:+.0f}</b>",
    ])
    if is_current_cycle and amount > 0 and mark > 0:
        pct = (mark / entry - 1) * 100 if entry > 0 else 0.0
        lines.append(
            f"Offen · <code>{amount:.4f}</code> @ Entry {_fmt_price(entry)} · "
            f"Mark {_fmt_price(mark)} · <code>{pct:+.1f}%</code>"
        )
    elif not is_current_cycle or amount <= 0:
        lines.append("<i>Position in diesem Zyklus geschlossen.</i>")

    return lines


def format_order_detail_rich(order: dict, *, scope: str | None = None) -> str:
    """Order detail with selected trade, full position trail, and PnL overview."""
    from notifications.coin_links import format_links_line

    scope = scope or order.get("ledger_scope") or "paper"
    selected_seq = int(order.get("display_seq") or 0)

    lines = _selected_order_block(order)
    sym = (order.get("symbol") or "").replace("/USDT", "")
    links = format_links_line(sym)
    if links:
        lines.append(links)

    if order.get("status") == "filled":
        lines.extend(_position_trail_block(order, scope=scope, selected_seq=selected_seq))
    else:
        lines.extend([
            "",
            "<i>Position-Trail nur für ausgeführte Orders (filled).</i>",
        ])

    return "\n".join(lines)