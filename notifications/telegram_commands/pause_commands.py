"""Telegram /pause, /resume, and /panic (#305 slice 3).

/pause flips only trading.entries_enabled. Stops, trails and exit ladders
keep running. /panic closes open longs through TradingService.execute_order
with signal SELL_STOP_FULL (emergency-sell bypass). Shorts are not covered.
Position locks (no_auto_sell) are listed and skipped.
"""

from __future__ import annotations

import threading
import time
import uuid
from html import escape as _esc
from typing import Any, Callable

from core.config import get_bot_config
from core.interactive_priority import interactive_priority
from core.models import TradeOrder, TradeResult
from core.tenant_context import tenant_context, tenant_snapshot
from logger import log
from notifications.telegram_commands.mode_commands import save_trading_flags
from notifications.telegram_commands.position_display import position_symbol
from notifications.telegram_i18n import money, signed_money, t
from notifications.telegram_commands.usage_hints import hint
from services.trading_service import TradingService
from storage.errors import LedgerUnavailable
from strategies.position_lock import MODE_NO_AUTO_SELL, is_position_locked
from strategies.short_math import is_short
from telegram_notifier import (
    answer_callback_query,
    send_telegram_buttons,
    send_telegram_message,
)

PANIC_TTL_SEC = 60.0
PANIC_PROGRESS_EVERY = 5
PANIC_SIGNAL = "SELL_STOP_FULL"

_clock: Callable[[], float] = time.monotonic
_pending_panic: dict[str, dict[str, Any]] = {}
_cmd_threads: list[threading.Thread] = []
_panic_guard = threading.Lock()


def reset_panic_for_tests(*, clock: Callable[[], float] | None = None) -> None:
    """Drop panic tokens, join leftover panic workers, optionally pin the clock."""
    global _clock
    threads = list(_cmd_threads)
    _cmd_threads.clear()
    for thread in threads:
        if thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5.0)
    _pending_panic.clear()
    _clock = clock or time.monotonic


def panic_running() -> bool:
    return any(t.is_alive() for t in _cmd_threads)


def _start_panic_thread(lots: list[dict[str, Any]]) -> bool:
    """Run the close-out off the webhook request thread (review #305 slice 3).

    The Telegram callback arrives inside a Flask request. Selling dozens of
    positions sequentially would block that worker for minutes and Telegram
    would redeliver the update. Same pattern as /positions (#329): tenant
    context is thread-local and re-entered in the worker; interactive_priority
    makes the price/eval loops yield while the panic runs.
    """
    with _panic_guard:
        if panic_running():
            send_telegram_message(t("panic_running"))
            return False
        tenant_id, scope, owner_chat_id = tenant_snapshot()
        sellable = sum(1 for row in lots if not row.get("locked"))
        token = interactive_priority()
        token.__enter__()

        def _run():
            try:
                with tenant_context(tenant_id, scope=scope, owner_chat_id=owner_chat_id):
                    _execute_panic(lots)
            except Exception as e:  # never die silently inside a panic
                log(f"panic execution failed: {e}", "ERROR")
                try:
                    send_telegram_message(t("panic_failed_unexpected", error=_esc(str(e))))
                except Exception:
                    pass
            finally:
                token.__exit__(None, None, None)

        thread = threading.Thread(target=_run, daemon=True, name="panic-cmd")
        _cmd_threads[:] = [th for th in _cmd_threads if th.is_alive()]
        _cmd_threads.append(thread)
        try:
            thread.start()
        except Exception:
            token.__exit__(None, None, None)
            raise
        send_telegram_message(t("panic_started", n=sellable))
        return True


def _now() -> float:
    return float(_clock())


def _switch_label(enabled: bool) -> str:
    return t("switch_on") if enabled else t("switch_off")


def _state_block(*, entries: bool, exits: bool) -> str:
    return t(
        "pause_state",
        entries=_switch_label(entries),
        exits=_switch_label(exits),
    )


def _current_exits_enabled() -> bool:
    try:
        return bool(get_bot_config().exits_enabled)
    except Exception:
        return True


def _create_panic_token(lots: list[dict[str, Any]], *, ttl: float = PANIC_TTL_SEC) -> str:
    token = uuid.uuid4().hex[:12]
    _pending_panic[token] = {
        "expires_at": _now() + float(ttl),
        "lots": lots,
    }
    return token


def consume_panic_token(token: str) -> list[dict[str, Any]] | None:
    rec = _pending_panic.pop(token, None)
    if rec is None:
        return None
    if _now() >= float(rec.get("expires_at") or 0):
        return None
    lots = rec.get("lots")
    return list(lots) if isinstance(lots, list) else []


def _estimate_closing_cost(symbol: str, price: float, amount: float, config) -> float:
    if price <= 0 or amount <= 0:
        return 0.0
    try:
        from core.costs import CostModel

        raw = getattr(config, "raw", config)
        fill = CostModel.from_config(raw, symbol=symbol).simulate_sell(price, amount)
        return float(fill.fee_usdt) + float(fill.slippage_usdt)
    except Exception:
        return 0.0


def collect_panic_lots(
    positions: list[dict] | None,
    prices: dict[str, float] | None,
    *,
    config=None,
) -> list[dict[str, Any]]:
    """Open LONGs only, notional-desc, with lock + PnL + expected close cost."""
    cfg = config if config is not None else get_bot_config()
    quotes = prices or {}
    lots: list[dict[str, Any]] = []
    for pos in positions or []:
        if not isinstance(pos, dict):
            continue
        if is_short(pos):
            continue
        symbol = position_symbol(pos)
        amount = float(pos.get("amount") or 0)
        if amount <= 0:
            continue
        price = float(quotes.get(symbol, 0) or pos.get("current_price") or 0)
        entry = float(pos.get("average_entry") or pos.get("entry_price") or 0)
        notional = amount * price if price > 0 else 0.0
        pnl = (price - entry) * amount if price > 0 and entry > 0 else 0.0
        locked = is_position_locked(pos, mode=MODE_NO_AUTO_SELL)
        cost = 0.0 if locked else _estimate_closing_cost(symbol, price, amount, cfg)
        lots.append(
            {
                "symbol": symbol,
                "timeframe": str(pos.get("timeframe") or "4h"),
                "amount": amount,
                "price": price,
                "entry": entry,
                "notional": notional,
                "pnl": pnl,
                "locked": bool(locked),
                "cost": cost,
            }
        )
    lots.sort(key=lambda row: float(row.get("notional") or 0), reverse=True)
    return lots


def _format_lot_line(idx: int, lot: dict[str, Any]) -> str:
    ticker = _esc(str(lot.get("symbol") or ""))
    amount = float(lot.get("amount") or 0)
    price = float(lot.get("price") or 0)
    pnl = float(lot.get("pnl") or 0)
    notional = float(lot.get("notional") or 0)
    line = (
        f"{idx}. <code>{ticker}</code>  Menge <code>{amount:.4f}</code>  "
        f"Kurs <b>${money(price, decimals=4 if price < 1 else 2)}</b>  "
        f"PnL <b>{signed_money(pnl)}</b>  (${money(notional)})"
    )
    if lot.get("locked"):
        line += f"  <i>{t('panic_locked')}</i>"
    return line


def _format_preview(lots: list[dict[str, Any]]) -> str:
    sellable = [row for row in lots if not row.get("locked")]
    locked_n = len(lots) - len(sellable)
    total_notional = sum(float(row.get("notional") or 0) for row in sellable)
    total_pnl = sum(float(row.get("pnl") or 0) for row in sellable)
    total_cost = sum(float(row.get("cost") or 0) for row in sellable)
    lines = [t("panic_title"), ""]
    for i, lot in enumerate(lots, 1):
        lines.append(_format_lot_line(i, lot))
    lines.append("")
    lines.append(
        t(
            "panic_total",
            notional=money(total_notional),
            pnl=signed_money(total_pnl),
            sellable=len(sellable),
            locked=locked_n,
        )
    )
    lines.append(t("panic_cost", cost=money(total_cost, decimals=2)))
    lines.append("")
    lines.append(t("panic_confirm_hint"))
    return "\n".join(lines)


def _preview_panic() -> bool:
    from price_fetcher import get_prices_batch
    from strategies.positions import list_active_positions

    active = list_active_positions()
    symbols = [position_symbol(p) for p in active if not is_short(p)]
    prices = get_prices_batch(symbols) if symbols else {}
    lots = collect_panic_lots(active, prices)
    if not lots:
        send_telegram_message(t("panic_empty"))
        return True
    token = _create_panic_token(lots)
    keyboard = [
        [
            {"text": t("panic_btn_close"), "callback_data": f"panic_ok:{token}"},
            {"text": t("panic_btn_cancel"), "callback_data": f"panic_no:{token}"},
        ]
    ]
    send_telegram_buttons(_format_preview(lots), keyboard)
    return True


def _execute_panic(lots: list[dict[str, Any]]) -> None:
    from price_fetcher import get_prices
    from strategies.positions import get_position

    sellable = [row for row in lots if not row.get("locked")]
    skipped = [row for row in lots if row.get("locked")]
    closed: list[str] = []
    failed: list[tuple[str, str]] = []
    remaining_after_stop: list[str] = []
    trading = TradingService()
    trading.refresh()
    n = len(sellable)
    for i, lot in enumerate(sellable, 1):
        symbol = str(lot.get("symbol") or "")
        timeframe = str(lot.get("timeframe") or "4h")
        try:
            pos = get_position(symbol, timeframe) or {}
            amount = float(pos.get("amount") or 0) or float(lot.get("amount") or 0)
        except Exception:
            amount = float(lot.get("amount") or 0)
        try:
            live = get_prices(symbol)[0]
            price = float(live) if live else float(lot.get("price") or 0)
        except Exception:
            price = float(lot.get("price") or 0)
        if amount <= 0 or price <= 0:
            failed.append((symbol, t("panic_fail_no_qty")))
            continue
        order = TradeOrder(
            type="SELL",
            symbol=symbol,
            price=price,
            amount=amount,
            signal=PANIC_SIGNAL,
            source="manual",
            exit_source="panic",
        )
        try:
            result = trading.execute_order(order, timeframe, source="manual")
        except LedgerUnavailable as exc:
            failed.append((symbol, f"ledger_unavailable: {exc}"))
            remaining_after_stop = [str(row.get("symbol") or "") for row in sellable[i:]]
            break
        code = str(getattr(result, "code", "") or "")
        if code == "ledger_unavailable" or (
            isinstance(result, TradeResult)
            and not result.executed
            and "ledger unavailable" in str(result.message or "").lower()
        ):
            failed.append((symbol, code or result.message or "ledger_unavailable"))
            remaining_after_stop = [str(row.get("symbol") or "") for row in sellable[i:]]
            break
        if result.executed:
            closed.append(symbol)
        else:
            failed.append((symbol, result.message or result.code or "failed"))
        if i % PANIC_PROGRESS_EVERY == 0 and i < n:
            send_telegram_message(t("panic_progress", done=i, total=n))

    save_trading_flags(entries_enabled=False)
    lines = [t("panic_summary")]
    if closed:
        lines.append(t("panic_closed_line", items=", ".join(closed), n=len(closed)))
    else:
        lines.append(t("panic_closed_line", items="—", n=0))
    if failed:
        bits = [f"{_esc(sym)} ({_esc(reason)})" for sym, reason in failed]
        lines.append(t("panic_failed_line", items="; ".join(bits), n=len(failed)))
    if skipped:
        names = ", ".join(_esc(str(row.get("symbol") or "")) for row in skipped)
        lines.append(t("panic_skipped_line", items=names, n=len(skipped)))
    if remaining_after_stop:
        lines.append(
            t(
                "panic_remaining_line",
                items=", ".join(_esc(s) for s in remaining_after_stop if s),
                n=len(remaining_after_stop),
            )
        )
    lines.append("")
    lines.append(
        t("panic_entries_off")
        + "\n"
        + _state_block(entries=False, exits=_current_exits_enabled())
    )
    send_telegram_message("\n".join(lines))


def handle(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    lower = raw.lower()
    if lower == "/pause":
        exits = _current_exits_enabled()
        if not save_trading_flags(entries_enabled=False):
            send_telegram_message(t("config_save_failed"))
            return True
        send_telegram_message(
            t("pause_done") + "\n\n" + _state_block(entries=False, exits=exits)
        )
        return True
    if lower == "/resume":
        exits = _current_exits_enabled()
        if not save_trading_flags(entries_enabled=True):
            send_telegram_message(t("config_save_failed"))
            return True
        send_telegram_message(
            t("resume_done") + "\n\n" + _state_block(entries=True, exits=exits)
        )
        return True
    if lower == "/panic":
        return _preview_panic()
    if lower.startswith("/pause ") or lower.startswith("/resume ") or lower.startswith("/panic "):
        key = lower.split()[0].lstrip("/")
        send_telegram_message(hint(key) if key in ("pause", "resume", "panic") else hint("unknown"))
        return True
    return False


def handle_callback(callback_query: dict) -> bool:
    data = str((callback_query or {}).get("data") or "")
    if not data.startswith("panic_"):
        return False
    answer_callback_query(callback_query.get("id"))
    parts = data.split(":", 1)
    if len(parts) != 2:
        send_telegram_message(t("panic_expired"))
        return True
    action, token = parts
    if action == "panic_no":
        _pending_panic.pop(token, None)
        send_telegram_message(t("panic_cancelled"))
        return True
    if action == "panic_ok":
        lots = consume_panic_token(token)
        if lots is None:
            send_telegram_message(t("panic_expired"))
            return True
        _start_panic_thread(lots)
        return True
    return True
