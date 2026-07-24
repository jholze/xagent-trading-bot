"""Telegram order lists: day trades, blocked, current-month trades (full lists)."""

from __future__ import annotations

from datetime import timedelta

from services.order_service import (
    OrderService,
    _orders_header_label,
    calendar_day_bounds,
    calendar_month_bounds,
    format_order_line,
    ledger_label,
)
from notifications.telegram_commands.order_detail_view import format_order_detail_rich
from notifications.telegram_commands.usage_hints import hint
from notifications.telegram_commands.utils import safe_int
from notifications.telegram_commands.command_context import activate_command
from notifications.telegram_i18n import t
from telegram_notifier import answer_callback_query, send_telegram_buttons, send_telegram_message

# View ids used in callback_data: orders_page:{view}:{scope}:{page}
VIEW_DAY = "day"
VIEW_BLOCKED = "blocked"
VIEW_MONTH = "month"
_VIEWS = frozenset({VIEW_DAY, VIEW_BLOCKED, VIEW_MONTH})

_TELEGRAM_CHUNK_LIMIT = 3900
_ORDER_BUTTON_CAP = 80
_BUTTONS_PER_ROW = 4


def _day_label() -> str:
    start, _ = calendar_day_bounds()
    return start.strftime("%d.%m.%Y")


def _month_label() -> str:
    start, _ = calendar_month_bounds()
    return start.strftime("%m.%Y")


def _fmt_usdt(value: float) -> str:
    v = float(value or 0)
    if abs(v) >= 1000:
        return f"${v:,.0f}"
    if abs(v) >= 100:
        return f"${v:.0f}"
    return f"${v:.1f}"


def _fmt_pnl(value: float) -> str:
    return f"${float(value or 0):+,.2f}"


def _perf_lines(stats: dict, *, period_label: str) -> list[str]:
    buys = int(stats.get("buys") or 0)
    sells = int(stats.get("sells") or 0)
    buy_usdt = float(stats.get("buy_usdt") or 0)
    sell_usdt = float(stats.get("sell_usdt") or 0)
    pnl = float(stats.get("realized_pnl") or 0)
    wins = int(stats.get("sell_wins") or 0)
    losses = int(stats.get("sell_losses") or 0)
    return [
        (
            f"🟢 {buys} Käufe ({_fmt_usdt(buy_usdt)}) · "
            f"🔴 {sells} Verkäufe ({_fmt_usdt(sell_usdt)})"
        ),
        f"{period_label}: <b>{_fmt_pnl(pnl)}</b>  ({wins}W / {losses}L)",
    ]


def _stats_header_day(ledger: OrderService) -> str:
    stats = ledger.stats_day_filled()
    scope = ledger.scope
    lines = [
        f"<b>📒 Trades heute — {_orders_header_label(scope)}</b>",
        f"<i>{_day_label()}</i>",
        *_perf_lines(stats, period_label="Tages-PnL"),
        "<i>Blockierte: <code>/orders_blocked</code> · Monat: <code>/orders_month</code></i>",
    ]
    return "\n".join(lines)


def _stats_header_blocked(ledger: OrderService) -> str:
    counts = ledger.stats_blocked_day()
    total = sum(counts.values())
    lines = [
        f"<b>🚫 Blockierte Orders — {_orders_header_label(ledger.scope)}</b>",
        f"<i>{_day_label()}</i> · {total} Einträge",
        (
            f"❌ {counts.get('rejected', 0)} blockiert · "
            f"⏳ {counts.get('pending_confirmation', 0)} offen · "
            f"🚫 {counts.get('cancelled', 0)} abgebrochen · "
            f"⌛ {counts.get('expired', 0)} abgelaufen · "
            f"⚠️ {counts.get('failed', 0)} fehlgeschlagen"
        ),
    ]
    top_codes = ledger.stats_blocked_day_codes(top=3)
    if top_codes:
        bits = " · ".join(f"<code>{code}</code> ×{n}" for code, n in top_codes)
        lines.append(f"Gründe: {bits}")
    lines.append("<i>Ausgeführte Trades: <code>/orders</code></i>")
    return "\n".join(lines)


def _stats_header_month(ledger: OrderService) -> str:
    start, end = calendar_month_bounds()
    stats = ledger.stats_month_filled()
    last_day = end - timedelta(days=1)
    lines = [
        f"<b>📅 Trades {_month_label()} — {_orders_header_label(ledger.scope)}</b>",
        f"<i>{start.strftime('%d.%m.')} – {last_day.strftime('%d.%m.%Y')}</i>",
        f"Σ {int(stats.get('filled') or 0)} ausgeführt",
        *_perf_lines(stats, period_label="Monats-PnL"),
        "<i>Heute: <code>/orders</code> · Blockiert: <code>/orders_blocked</code></i>",
    ]
    return "\n".join(lines)


def _order_number_buttons(
    view: str,
    scope: str,
    orders: list[dict],
    *,
    page: int = 1,
    cap: int = _ORDER_BUTTON_CAP,
) -> list[list[dict]]:
    if not orders:
        return []
    rows: list[list[dict]] = []
    row: list[dict] = []
    for order in orders[: max(0, cap)]:
        seq = order.get("display_seq")
        if not seq:
            continue
        side = (order.get("side") or "?")[0].upper()
        row.append({
            "text": f"#{seq} {side}",
            "callback_data": f"order_detail:{scope}:{seq}:{page}:{view}",
        })
        if len(row) >= _BUTTONS_PER_ROW:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def _detail_back_buttons(view: str, scope: str, page: int = 1) -> list[list[dict]]:
    labels = {
        VIEW_DAY: "◀ Trades heute",
        VIEW_BLOCKED: "◀ Blockiert",
        VIEW_MONTH: "◀ Monat",
    }
    return [[
        {
            "text": labels.get(view, "◀ Orderbuch"),
            # page ignored for full-list views; kept for callback compatibility
            "callback_data": f"orders_page:{view}:{scope}:{page}",
        },
    ]]


def _empty_message(view: str) -> str:
    if view == VIEW_BLOCKED:
        return "<i>Keine blockierten Orders heute.</i>"
    if view == VIEW_MONTH:
        return f"<i>Keine ausgeführten Trades im {_month_label()}.</i>"
    return f"<i>Keine ausgeführten Trades am {_day_label()}.</i>"


def _fetch_all(ledger: OrderService, view: str) -> list:
    if view == VIEW_BLOCKED:
        return ledger.list_blocked_day_all()
    if view == VIEW_MONTH:
        return ledger.list_month_filled_all()
    return ledger.list_day_filled_all()


def _header_for_view(ledger: OrderService, view: str) -> str:
    if view == VIEW_BLOCKED:
        return _stats_header_blocked(ledger)
    if view == VIEW_MONTH:
        return _stats_header_month(ledger)
    return _stats_header_day(ledger)


def _chunk_lines(header: str, body_lines: list[str], *, limit: int = _TELEGRAM_CHUNK_LIMIT) -> list[str]:
    """Split header + body lines into Telegram-safe HTML chunks."""
    if not body_lines:
        return [header] if header else []

    chunks: list[str] = []
    # First chunk starts with header
    current = header.rstrip() + "\n\n" if header else ""
    for line in body_lines:
        candidate = (current + line + "\n") if current else (line + "\n")
        if len(candidate) <= limit:
            current = candidate
            continue
        if current.strip():
            chunks.append(current.rstrip())
        # single line longer than limit — hard split
        if len(line) > limit:
            start = 0
            while start < len(line):
                piece = line[start : start + limit]
                chunks.append(piece)
                start += limit
            current = ""
        else:
            current = line + "\n"
    if current.strip():
        chunks.append(current.rstrip())
    if len(chunks) > 1:
        total = len(chunks)
        tagged = []
        for i, ch in enumerate(chunks):
            tagged.append(f"{ch}\n\n<i>({i + 1}/{total})</i>")
        return tagged
    return chunks or [header]


def send_orders_view(view: str = VIEW_DAY, page: int = 1) -> None:
    """Render a full-list order view (day / blocked / month). ``page`` ignored."""
    view = view if view in _VIEWS else VIEW_DAY
    ledger = OrderService()
    orders = _fetch_all(ledger, view)
    header = _header_for_view(ledger, view)

    if not orders:
        msg = header + "\n\n" + _empty_message(view)
        send_telegram_message(msg)
        return

    show_reason = view == VIEW_BLOCKED
    body = [
        format_order_line(o, show_block_reason=show_reason)
        for o in orders
    ]
    footer_bits = [
        "",
        "<i>Tippe eine <b>Ordernummer</b> / Button für Details</i>",
    ]
    if len(orders) > _ORDER_BUTTON_CAP:
        footer_bits.append(
            f"<i>Buttons: erste {_ORDER_BUTTON_CAP} · Detail: <code>/orders #</code></i>"
        )
    body.extend(footer_bits)

    chunks = _chunk_lines(header, body, limit=_TELEGRAM_CHUNK_LIMIT)
    buttons = _order_number_buttons(view, ledger.scope, orders, page=1)

    if len(chunks) == 1:
        if buttons:
            send_telegram_buttons(chunks[0], buttons)
        else:
            send_telegram_message(chunks[0])
        return

    for ch in chunks[:-1]:
        send_telegram_message(ch)
    if buttons:
        send_telegram_buttons(chunks[-1], buttons)
    else:
        send_telegram_message(chunks[-1])


def send_orders_page(page: int = 1) -> None:
    """Backward-compatible: /orders day view (full list)."""
    send_orders_view(VIEW_DAY, page)


def send_order_detail(
    display_seq: int,
    *,
    list_page: int = 1,
    view: str = VIEW_DAY,
) -> None:
    ledger = OrderService()
    order = ledger.get_by_display_seq(display_seq)
    if not order:
        send_telegram_message(t("order_not_found", seq=display_seq, ledger=ledger_label()))
        return
    msg = format_order_detail_rich(order, scope=ledger.scope)
    v = view if view in _VIEWS else VIEW_DAY
    send_telegram_buttons(msg, _detail_back_buttons(v, ledger.scope, list_page))


def _handle_page_arg(parts: list[str], view: str) -> bool:
    # Legacy: /orders page N — still accepted, shows full list (page ignored).
    page = safe_int(parts[2], default=1) if len(parts) > 2 else 1
    if page is None or page < 1:
        key = "orders" if view == VIEW_DAY else (
            "orders_blocked" if view == VIEW_BLOCKED else "orders_month"
        )
        send_telegram_message(hint(key))
        return True
    send_orders_view(view, page)
    return True


def handle(text: str) -> bool:
    raw = (text or "").strip()
    lower = raw.lower()

    # --- /orders_blocked ---
    if lower == "/orders_blocked" or lower.startswith("/orders_blocked "):
        parts = [p.strip() for p in raw.split() if p.strip()]
        if len(parts) == 1:
            send_orders_view(VIEW_BLOCKED, 1)
            return True
        if len(parts) >= 2 and parts[1].lower() == "page":
            return _handle_page_arg(parts, VIEW_BLOCKED)
        activate_command("orders_blocked")
        send_telegram_message(hint("orders_blocked"))
        return True

    # --- /orders_month ---
    if lower == "/orders_month" or lower.startswith("/orders_month "):
        parts = [p.strip() for p in raw.split() if p.strip()]
        if len(parts) == 1:
            send_orders_view(VIEW_MONTH, 1)
            return True
        if len(parts) >= 2 and parts[1].lower() == "page":
            return _handle_page_arg(parts, VIEW_MONTH)
        activate_command("orders_month")
        send_telegram_message(hint("orders_month"))
        return True

    # --- /orders and /order (today filled) ---
    if lower in ("/orders", "/order"):
        send_orders_view(VIEW_DAY, 1)
        return True

    if not (lower.startswith("/orders ") or lower.startswith("/order ")):
        return False

    parts = [p.strip() for p in raw.split() if p.strip()]
    if len(parts) < 2:
        activate_command("orders")
        send_telegram_message(hint("orders"))
        return True

    if parts[1].lower() == "page":
        return _handle_page_arg(parts, VIEW_DAY)

    seq = safe_int(parts[1])
    if seq is None or seq < 1:
        send_telegram_message(hint("orders"))
        return True
    send_order_detail(seq, view=VIEW_DAY)
    return True


def handle_callback(callback_query: dict) -> bool:
    data = callback_query.get("data", "")

    if data.startswith("orders_page:"):
        answer_callback_query(callback_query.get("id"))
        parts = data.split(":")
        # Legacy: orders_page:{scope}:{page}
        # New:     orders_page:{view}:{scope}:{page}
        view = VIEW_DAY
        scope = None
        page = 1
        if len(parts) == 3:
            scope = parts[1]
            page = safe_int(parts[2], default=1) or 1
        elif len(parts) == 4:
            view = parts[1] if parts[1] in _VIEWS else VIEW_DAY
            scope = parts[2]
            page = safe_int(parts[3], default=1) or 1
        else:
            return True

        if page < 1:
            page = 1
        ledger = OrderService()
        if scope != ledger.scope:
            send_telegram_message(t("order_page_oob"))
            return True
        send_orders_view(view, page)
        return True

    if data.startswith("order_detail:"):
        answer_callback_query(callback_query.get("id"))
        parts = data.split(":")
        # order_detail:{scope}:{seq}[:{page}[:{view}]]
        if len(parts) < 3:
            return True
        scope = parts[1]
        seq = safe_int(parts[2])
        list_page = safe_int(parts[3], default=1) if len(parts) > 3 else 1
        view = parts[4] if len(parts) > 4 and parts[4] in _VIEWS else VIEW_DAY
        if seq is None or seq < 1:
            return True

        ledger = OrderService()
        if scope != ledger.scope:
            send_telegram_message(t("order_page_oob"))
            return True

        send_order_detail(seq, list_page=list_page or 1, view=view)
        return True

    return False
