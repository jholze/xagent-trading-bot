"""Telegram order lists: day trades, blocked, current-month trades."""

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


def _day_label() -> str:
    start, _ = calendar_day_bounds()
    return start.strftime("%d.%m.%Y")


def _month_label() -> str:
    start, _ = calendar_month_bounds()
    return start.strftime("%m.%Y")


def _stats_header_day(ledger: OrderService) -> str:
    executed = ledger.stats_day_filled()
    scope = ledger.scope
    lines = [
        f"<b>📒 Trades heute — {_orders_header_label(scope)}</b>",
        f"<i>{_day_label()}</i>",
        (
            f"Heute ausgeführt: 🟢 {executed['buys']} Käufe · "
            f"🔴 {executed['sells']} Verkäufe"
        ),
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
        "<i>Ausgeführte Trades: <code>/orders</code></i>",
    ]
    return "\n".join(lines)


def _stats_header_month(ledger: OrderService) -> str:
    start, end = calendar_month_bounds()
    orders, _ = ledger.list_month_filled(page=1, per_page=10_000)
    buys = sum(1 for o in orders if (o.get("side") or "").lower() == "buy")
    sells = sum(1 for o in orders if (o.get("side") or "").lower() == "sell")
    last_day = end - timedelta(days=1)
    lines = [
        f"<b>📅 Trades {_month_label()} — {_orders_header_label(ledger.scope)}</b>",
        f"<i>{start.strftime('%d.%m.')} – {last_day.strftime('%d.%m.%Y')}</i>",
        f"Monat ausgeführt: 🟢 {buys} Käufe · 🔴 {sells} Verkäufe · Σ {len(orders)}",
        "<i>Heute: <code>/orders</code> · Blockiert: <code>/orders_blocked</code></i>",
    ]
    return "\n".join(lines)


def _pagination_buttons(view: str, scope: str, page: int, total_pages: int) -> list[list[dict]]:
    row = []
    if page > 1:
        row.append({
            "text": "◀ Zurück",
            "callback_data": f"orders_page:{view}:{scope}:{page - 1}",
        })
    if page < total_pages:
        row.append({
            "text": "Weiter ▶",
            "callback_data": f"orders_page:{view}:{scope}:{page + 1}",
        })
    return [row] if row else []


def _order_number_buttons(view: str, scope: str, orders: list[dict], page: int) -> list[list[dict]]:
    if not orders:
        return []
    row = []
    for order in orders:
        seq = order.get("display_seq")
        if not seq:
            continue
        side = (order.get("side") or "?")[0].upper()
        row.append({
            "text": f"#{seq} {side}",
            "callback_data": f"order_detail:{scope}:{seq}:{page}:{view}",
        })
    return [row] if row else []


def _detail_back_buttons(view: str, scope: str, page: int = 1) -> list[list[dict]]:
    labels = {
        VIEW_DAY: "◀ Trades heute",
        VIEW_BLOCKED: "◀ Blockiert",
        VIEW_MONTH: "◀ Monat",
    }
    return [[
        {
            "text": labels.get(view, "◀ Orderbuch"),
            "callback_data": f"orders_page:{view}:{scope}:{page}",
        },
    ]]


def _empty_message(view: str) -> str:
    if view == VIEW_BLOCKED:
        return "<i>Keine blockierten Orders heute.</i>"
    if view == VIEW_MONTH:
        return f"<i>Keine ausgeführten Trades im {_month_label()}.</i>"
    return f"<i>Keine ausgeführten Trades am {_day_label()}.</i>"


def _fetch_page(ledger: OrderService, view: str, page: int) -> tuple[list, int]:
    if view == VIEW_BLOCKED:
        return ledger.list_blocked_orders(page=page)
    if view == VIEW_MONTH:
        return ledger.list_month_filled(page=page)
    return ledger.list_day_filled(page=page)


def _header_for_view(ledger: OrderService, view: str) -> str:
    if view == VIEW_BLOCKED:
        return _stats_header_blocked(ledger)
    if view == VIEW_MONTH:
        return _stats_header_month(ledger)
    return _stats_header_day(ledger)


def send_orders_view(view: str = VIEW_DAY, page: int = 1) -> None:
    view = view if view in _VIEWS else VIEW_DAY
    page = max(1, int(page or 1))
    ledger = OrderService()
    orders, total_pages = _fetch_page(ledger, view, page)
    if page > total_pages:
        page = total_pages
        orders, total_pages = _fetch_page(ledger, view, page)

    lines = [_header_for_view(ledger, view), ""]
    if not orders:
        lines.append(_empty_message(view))
    else:
        lines.append(
            f"<b>Seite {page}/{total_pages}</b> — Tippe eine <b>Ordernummer</b> für Details"
        )
        lines.append("")
        for order in orders:
            lines.append(format_order_line(order))
    msg = "\n".join(lines)

    buttons: list[list[dict]] = []
    num_row = _order_number_buttons(view, ledger.scope, orders, page)
    if num_row:
        buttons.extend(num_row)
    buttons.extend(_pagination_buttons(view, ledger.scope, page, total_pages))

    if buttons:
        send_telegram_buttons(msg, buttons)
    else:
        send_telegram_message(msg)


def send_orders_page(page: int = 1) -> None:
    """Backward-compatible: /orders day view."""
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
    page = safe_int(parts[2], default=1) if len(parts) > 2 else 1
    if page is None or page < 1:
        send_telegram_message(hint("orders" if view == VIEW_DAY else f"orders_{view}" if view != VIEW_DAY else "orders"))
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

    # --- /orders_month (Weiter / Monats-Trades) ---
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

    # --- /orders (today filled) ---
    if lower == "/orders":
        send_orders_view(VIEW_DAY, 1)
        return True

    if not lower.startswith("/orders "):
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
