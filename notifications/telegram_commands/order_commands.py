"""Telegram /orders — paginated order ledger."""

from __future__ import annotations

from services.order_service import (
    ORDERS_PER_PAGE,
    OrderService,
    format_order_line,
    ledger_label,
)
from notifications.telegram_commands.order_detail_view import format_order_detail_rich
from notifications.telegram_commands.usage_hints import hint
from notifications.telegram_commands.utils import safe_int
from notifications.telegram_commands.command_context import activate_command
from telegram_notifier import answer_callback_query, send_telegram_buttons, send_telegram_message


def _stats_header(ledger: OrderService) -> str:
    executed = ledger.stats_executed_24h()
    attempts = ledger.stats_24h()
    scope = ledger.scope
    blocked = (
        attempts["rejected"]
        + attempts["cancelled"]
        + attempts["pending_confirmation"]
        + attempts["expired"]
        + attempts["failed"]
        + attempts["executing"]
    )
    lines = [
        f"<b>📒 Orderbuch — {ledger_label(scope)}</b>",
        (
            f"24h ausgeführt: 🟢 {executed['buys']} Käufe · "
            f"🔴 {executed['sells']} Verkäufe"
        ),
    ]
    if blocked:
        lines.append(
            f"<i>Nicht ausgeführt (24h):</i> ❌ {attempts['rejected']} blockiert · "
            f"⏳ {attempts['pending_confirmation']} offen · "
            f"🚫 {attempts['cancelled']} abgebrochen · "
            f"⌛ {attempts['expired']} abgelaufen · "
            f"⚠️ {attempts['failed']} fehlgeschlagen"
        )
    return "\n".join(lines)


def _pagination_buttons(scope: str, page: int, total_pages: int) -> list[list[dict]]:
    row = []
    if page > 1:
        row.append({"text": "◀ Zurück", "callback_data": f"orders_page:{scope}:{page - 1}"})
    if page < total_pages:
        row.append({"text": "Weiter ▶", "callback_data": f"orders_page:{scope}:{page + 1}"})
    return [row] if row else []


def _order_number_buttons(scope: str, orders: list[dict]) -> list[list[dict]]:
    """One clickable row of order numbers for the current page."""
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
            "callback_data": f"order_detail:{scope}:{seq}",
        })
    return [row] if row else []


def _detail_back_buttons(scope: str, page: int = 1) -> list[list[dict]]:
    return [[
        {"text": "◀ Orderbuch", "callback_data": f"orders_page:{scope}:{page}"},
    ]]


def send_orders_page(page: int = 1) -> None:
    ledger = OrderService()
    orders, total_pages = ledger.list_orders(page=page, trade_book_only=True)
    lines = [_stats_header(ledger), ""]
    if not orders:
        lines.append("<i>Keine ausgeführten Trades in diesem Orderbuch.</i>")
    else:
        lines.append(
            f"<b>Seite {page}/{total_pages}</b> — Tippe eine <b>Ordernummer</b> für Details + Trail"
        )
        lines.append("")
        for order in orders:
            lines.append(format_order_line(order))
    msg = "\n".join(lines)

    buttons: list[list[dict]] = []
    num_row = _order_number_buttons(ledger.scope, orders)
    if num_row:
        buttons.extend(num_row)
    buttons.extend(_pagination_buttons(ledger.scope, page, total_pages))

    if buttons:
        send_telegram_buttons(msg, buttons)
    else:
        send_telegram_message(msg)


def send_order_detail(display_seq: int, *, list_page: int = 1) -> None:
    ledger = OrderService()
    order = ledger.get_by_display_seq(display_seq)
    if not order:
        send_telegram_message(f"❌ Order <b>#{display_seq}</b> nicht gefunden im {ledger_label()} Ledger.")
        return
    msg = format_order_detail_rich(order, scope=ledger.scope)
    send_telegram_buttons(msg, _detail_back_buttons(ledger.scope, list_page))


def handle(text: str) -> bool:
    if text == "/orders":
        send_orders_page(1)
        return True

    if not text.startswith("/orders "):
        return False

    parts = [p.strip() for p in text.split() if p.strip()]
    if len(parts) < 2:
        activate_command("orders")
        send_telegram_message(hint("orders"))
        return True

    if parts[1].lower() == "page":
        page = safe_int(parts[2], default=1) if len(parts) > 2 else 1
        if page is None or page < 1:
            send_telegram_message(hint("orders"))
            return True
        send_orders_page(page)
        return True

    seq = safe_int(parts[1])
    if seq is None or seq < 1:
        send_telegram_message(hint("orders"))
        return True
    send_order_detail(seq)
    return True


def handle_callback(callback_query: dict) -> bool:
    data = callback_query.get("data", "")
    if data.startswith("orders_page:"):
        answer_callback_query(callback_query.get("id"))
        parts = data.split(":")
        if len(parts) != 3:
            return True

        page = safe_int(parts[2], default=1)
        if page is None or page < 1:
            page = 1

        ledger = OrderService()
        if parts[1] != ledger.scope:
            send_telegram_message(
                f"⚠️ Ledger-Scope hat sich geändert ({ledger_label(parts[1])} → {ledger_label()}). "
                "Sende <code>/orders</code> erneut."
            )
            return True

        send_orders_page(page)
        return True

    if data.startswith("order_detail:"):
        answer_callback_query(callback_query.get("id"))
        parts = data.split(":")
        if len(parts) < 3:
            return True
        scope = parts[1]
        seq = safe_int(parts[2])
        list_page = safe_int(parts[3], default=1) if len(parts) > 3 else 1
        if seq is None or seq < 1:
            return True

        ledger = OrderService()
        if scope != ledger.scope:
            send_telegram_message(
                f"⚠️ Ledger-Scope hat sich geändert ({ledger_label(scope)} → {ledger_label()}). "
                "Sende <code>/orders</code> erneut."
            )
            return True

        send_order_detail(seq, list_page=list_page or 1)
        return True

    return False