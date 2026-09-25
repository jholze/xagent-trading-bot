import threading

from core.interactive_priority import interactive_priority
from core.tenant_context import tenant_context, tenant_snapshot
from notifications.telegram_commands.command_context import current_chat_id, set_chat_id
from notifications.telegram_commands.menu_i18n import current_language, set_user_language
from notifications.telegram_commands.position_display import (
    LOT_BUYS_CALLBACK_PREFIX,
    LOT_SHEET_CALLBACK_PREFIX,
    format_lot_buys_message,
    format_lot_sheet_message,
    live_price_for_one_symbol,
    lot_sheet_keyboard,
    one_line_why_for_symbol,
    parse_lot_callback,
    position_symbol,
    resolve_position_by_symbol_tf,
    send_positions_snapshot,
)
from notifications.telegram_i18n import t
from telegram_notifier import answer_callback_query, send_telegram_buttons, send_telegram_message

_cmd_threads: list[threading.Thread] = []
_COMPACT_COMMANDS = {"/positions", "/portfolio", "/status", "/balance"}
_FULL_COMMANDS = {
    "/positions full",
    "/positions detail",
    "/positions_full",
    "/portfolio full",
    "/portfolio detail",
}
POS_MORE_PREFIX = "pos_more:"


def _is_mongo_client_closed_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "after close" in msg or (
        exc.__class__.__name__ == "InvalidOperation" and "mongoclient" in msg
    )


def _build_positions(
    chat_id: str,
    *,
    detail_level: str,
    tenant_id: str,
    scope: str,
    owner_chat_id: str,
    lang: str,
):
    set_user_language(lang)
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            with tenant_context(tenant_id, scope=scope, owner_chat_id=owner_chat_id):
                send_positions_snapshot(
                    fast=True,
                    chat_id=chat_id or None,
                    detail_level=detail_level,
                    tenant_id=tenant_id,
                    scope=scope,
                )
            return
        except Exception as e:
            last_err = e
            if attempt == 0 and _is_mongo_client_closed_error(e):
                # Shared client was closed mid-request — drop + reopen, retry once.
                try:
                    from storage.mongo_client import close_client

                    close_client()
                except Exception:
                    pass
                continue
            break
    set_user_language(lang)
    send_telegram_message(
        t("portfolio_load_failed", error=last_err),
        chat_id=chat_id or None,
    )


def handle(text: str) -> bool:
    if text in _COMPACT_COMMANDS:
        detail_level = "compact"
        loading = t("portfolio_loading_compact")
    elif text in _FULL_COMMANDS:
        detail_level = "full"
        loading = t("portfolio_loading_full")
    else:
        return False

    chat_id = current_chat_id()
    tenant_id, scope, owner_chat_id = tenant_snapshot()
    lang = current_language()
    send_telegram_message(loading, chat_id=chat_id or None)
    # Raise the flag before the worker starts so eval/cycle yield immediately.
    token = interactive_priority()
    token.__enter__()

    def _run():
        try:
            _build_positions(
                chat_id,
                detail_level=detail_level,
                tenant_id=tenant_id,
                scope=scope,
                owner_chat_id=owner_chat_id,
                lang=lang,
            )
        finally:
            token.__exit__(None, None, None)

    try:
        thread = threading.Thread(
            target=_run,
            daemon=True,
            name="positions-cmd",
        )
        _cmd_threads[:] = [t for t in _cmd_threads if t.is_alive()]
        _cmd_threads.append(thread)
        thread.start()
    except Exception:
        token.__exit__(None, None, None)
        raise
    return True


def handle_callback(callback_query: dict) -> bool:
    """Compact /positions lot sheet (#561) and Mehr Details (#447)."""
    data = str((callback_query or {}).get("data") or "").strip()
    if data.startswith(LOT_SHEET_CALLBACK_PREFIX):
        return _handle_poslot_callback(callback_query)
    if data.startswith(LOT_BUYS_CALLBACK_PREFIX):
        return _handle_posbuys_callback(callback_query)
    if not data.startswith(POS_MORE_PREFIX):
        return False
    callback_id = callback_query.get("id")
    if callback_id:
        answer_callback_query(callback_id)
    level = data[len(POS_MORE_PREFIX):].strip().lower()
    if level != "full":
        return True
    message = callback_query.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    if chat_id is not None:
        set_chat_id(chat_id)
    return handle("/positions full")


def _lot_callback_chat_id(callback_query: dict, log_label: str):
    """Ack first, then refuse when chat.id is absent. Never clear_context()."""
    from logger import log

    answer_callback_query(callback_query.get("id"))
    message = callback_query.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    if not chat_id:
        log(f"{log_label} callback missing chat id", "WARNING")
        return None
    set_chat_id(chat_id)
    return chat_id


def _resolve_open_lot(ticker: str, timeframe: str):
    from strategies.positions import list_active_positions

    return resolve_position_by_symbol_tf(list_active_positions(), ticker, timeframe)


def _handle_poslot_callback(callback_query: dict) -> bool:
    chat_id = _lot_callback_chat_id(callback_query, "poslot")
    if not chat_id:
        return True
    data = str(callback_query.get("data") or "")
    parsed = parse_lot_callback(data, LOT_SHEET_CALLBACK_PREFIX)
    if parsed is None:
        send_telegram_message(t("no_open_position", arg=""), chat_id=chat_id)
        return True
    ticker, tf = parsed
    p = _resolve_open_lot(ticker, tf)
    if not p:
        send_telegram_message(t("no_open_position", arg=ticker), chat_id=chat_id)
        return True
    sym = position_symbol(p)
    price = live_price_for_one_symbol(sym)
    if price <= 0:
        price = float(p.get("average_entry", p.get("entry_price", 0)) or 0)
    why = one_line_why_for_symbol(sym)
    send_telegram_buttons(
        format_lot_sheet_message(p, price, why),
        lot_sheet_keyboard(p),
        chat_id=chat_id,
    )
    return True


def _handle_posbuys_callback(callback_query: dict) -> bool:
    chat_id = _lot_callback_chat_id(callback_query, "posbuys")
    if not chat_id:
        return True
    data = str(callback_query.get("data") or "")
    parsed = parse_lot_callback(data, LOT_BUYS_CALLBACK_PREFIX)
    if parsed is None:
        send_telegram_message(t("no_open_position", arg=""), chat_id=chat_id)
        return True
    ticker, tf = parsed
    p = _resolve_open_lot(ticker, tf)
    if not p:
        send_telegram_message(t("no_open_position", arg=ticker), chat_id=chat_id)
        return True
    sym = position_symbol(p)
    price = live_price_for_one_symbol(sym)
    if price <= 0:
        price = float(p.get("average_entry", p.get("entry_price", 0)) or 0)
    send_telegram_message(format_lot_buys_message(p, price), chat_id=chat_id)
    return True


def reset_portfolio_commands_for_tests() -> None:
    """Join leftover /positions worker threads (pytest workers, #329)."""
    threads = list(_cmd_threads)
    _cmd_threads.clear()
    for thread in threads:
        if thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
