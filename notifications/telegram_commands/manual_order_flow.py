"""Manual /buy, /sell, /short and /cover confirmation flow with risk preview.

Every manual order goes preview -> ``manual_ok`` / ``manual_no`` tap. Nothing
here calls ``execute_*`` before the operator confirmed (#452).
"""

from __future__ import annotations

from typing import Optional

from core.models import TradeOrder
from services.order_service import OrderService
from services.trading_service import TradingService
from strategies.positions import get_position
from strategies.short_math import is_short, margin_usdt, snapshot as short_snapshot
from notifications.telegram_i18n import t
from telegram_notifier import answer_callback_query, send_telegram_buttons, send_telegram_message


def _ticker(symbol: str) -> str:
    return symbol.replace("/USDT", "").split("/")[0]


def _format_price(price: float) -> str:
    from price_fetcher import format_usdt_price

    return format_usdt_price(price)


def _format_buy_preview(
    decision,
    status: dict,
    *,
    symbol: str,
    price: float,
    requested_usdt: float,
) -> str:
    approved_usdt = float(decision.order.usdt_amount)
    est_amount = approved_usdt / price if price > 0 else 0
    from notifications.coin_links import format_links_line, format_ticker_html

    ticker = _ticker(symbol)
    ticker_html = format_ticker_html(ticker, symbol_suffix="")
    links = format_links_line(ticker)
    lines = [
        f"<b>🛡️ Risiko-Prüfung — Kauf {ticker_html}</b>",
    ]
    if links:
        lines.append(links)
    lines.extend([
        "",
        f"Kurs <b>{_format_price(price)}</b>",
        f"Angefragt <b>${requested_usdt:.0f}</b> USDT",
    ])
    if abs(approved_usdt - requested_usdt) > 0.01:
        reason = []
        if approved_usdt < status.get("virtual_balance", approved_usdt):
            reason.append("Cash-Limit")
        reason.append(f"Max. {status.get('max_position_percent', 30):.0f}% pro Coin")
        note = " · ".join(reason)
        lines.append(f"Freigegeben <b>${approved_usdt:.0f}</b> USDT <i>(angepasst: {note})</i>")
    else:
        lines.append(f"Freigegeben <b>${approved_usdt:.0f}</b> USDT")
    lines.append(f"Geschätzte Menge <code>~{est_amount:.4f}</code> {_ticker(symbol)}")
    lines.extend(_portfolio_block(status))
    lines.extend(_hint_block(decision, status, kind="buy"))
    lines.append("")
    lines.append("<b>Order ausführen?</b>")
    return "\n".join(lines)


def _format_sell_preview(
    decision,
    status: dict,
    *,
    symbol: str,
    price: float,
    amount: float,
    pct: float,
    timeframe: str,
) -> str:
    pos = get_position(symbol, timeframe)
    total = float(pos.get("amount", 0))
    entry = float(pos.get("average_entry", pos.get("entry_price", 0)) or 0)
    est_usdt = amount * price
    from notifications.coin_links import format_links_line, format_ticker_html

    ticker = _ticker(symbol)
    ticker_html = format_ticker_html(ticker, symbol_suffix="")
    links = format_links_line(ticker)
    lines = [
        f"<b>🛡️ Risiko-Prüfung — Verkauf {ticker_html}</b>",
    ]
    if links:
        lines.append(links)
    lines.extend([
        "",
        f"Anteil <b>{pct * 100:.0f}%</b> der Position",
        f"Kurs <b>{_format_price(price)}</b>",
        f"Menge <code>{amount:.4f}</code> {_ticker(symbol)} · ca. <b>${est_usdt:.0f}</b>",
    ])
    if total > 0:
        lines.append(f"Position <code>{total:.4f}</code> @ Entry {_format_price(entry)}")
    lines.extend(_portfolio_block(status))
    lines.extend(_hint_block(decision, status, kind="sell"))
    lines.append("")
    lines.append("<b>Order ausführen?</b>")
    return "\n".join(lines)


def _format_short_preview(
    decision,
    status: dict,
    *,
    symbol: str,
    price: float,
    requested_usdt: Optional[float],
    requested_leverage: Optional[float],
    timeframe: str,
) -> str:
    """Paper short preview. Defaults (size, leverage) are spelled out — the
    operator must see what a bare ``/short H`` would open (#452)."""
    approved = decision.order
    approved_usdt = float(approved.usdt_amount or 0)
    lev = float(approved.leverage or 1.0)
    qty = float(approved.qty or 0) or (approved_usdt / price if price > 0 else 0)
    margin = margin_usdt(qty, price, lev)
    liq = float(
        short_snapshot(
            {"side": "short", "amount": qty, "average_entry": price, "leverage": lev},
            price,
            cap=max(lev, 1.0),
        ).get("liq_price")
        or 0
    )
    from notifications.coin_links import format_links_line, format_ticker_html

    ticker = _ticker(symbol)
    ticker_html = format_ticker_html(ticker, symbol_suffix="")
    links = format_links_line(ticker)
    lines = [f"<b>🛡️ Risiko-Prüfung — 🔻 Short {ticker_html}</b> <i>(Paper)</i>"]
    if links:
        lines.append(links)
    lines.extend(["", f"Kurs <b>{_format_price(price)}</b>"])
    if requested_usdt is None:
        lines.append(
            f"Notional <b>${approved_usdt:.0f}</b> USDT "
            f"<i>(Standardgröße — kein Betrag angegeben)</i>"
        )
    elif abs(approved_usdt - float(requested_usdt)) > 0.01:
        lines.append(
            f"Angefragt <b>${float(requested_usdt):.0f}</b> USDT · "
            f"Freigegeben <b>${approved_usdt:.0f}</b> USDT <i>(angepasst)</i>"
        )
    else:
        lines.append(f"Notional <b>${approved_usdt:.0f}</b> USDT")
    if requested_leverage is None:
        lines.append(f"Hebel <b>{lev:g}×</b> <i>(Standard — kein Hebel angegeben)</i>")
    elif abs(lev - float(requested_leverage)) > 1e-9:
        lines.append(
            f"Hebel <b>{lev:g}×</b> <i>(angefragt {float(requested_leverage):g}×, gekappt)</i>"
        )
    else:
        lines.append(f"Hebel <b>{lev:g}×</b>")
    lines.append(f"Geschätzte Menge <code>~{qty:.4f}</code> {ticker}")
    lines.append(f"Margin ≈ <b>${margin:.0f}</b> · Liq ≈ {_format_price(liq)}")
    pos = get_position(symbol, timeframe)
    if is_short(pos) and float(pos.get("amount") or 0) > 0:
        lines.append(
            f"Bestehender Short <code>{float(pos.get('amount') or 0):.4f}</code> "
            f"@ Entry {_format_price(float(pos.get('average_entry') or 0))} — wird aufgestockt"
        )
    lines.extend(_portfolio_block(status))
    lines.extend(_hint_block(decision, status, kind="short"))
    lines.append("")
    lines.append("<b>Short eröffnen?</b>")
    return "\n".join(lines)


def _format_cover_preview(
    decision,
    status: dict,
    *,
    symbol: str,
    price: float,
    amount: float,
    pct: float,
    timeframe: str,
) -> str:
    """Cover preview. ``pct`` is a fraction (1.0 = full cover); the default is
    shown explicitly so a bare ``/cover H`` is never a silent 100 % (#452)."""
    pos = get_position(symbol, timeframe) or {}
    total = float(pos.get("amount") or 0)
    entry = float(pos.get("average_entry") or 0)
    try:
        lev = max(1.0, float(pos.get("leverage") or 1.0))
    except (TypeError, ValueError):
        lev = 1.0
    est_usdt = amount * price
    est_pnl = amount * (entry - price)
    margin_freed = margin_usdt(amount, entry, lev)
    from notifications.coin_links import format_links_line, format_ticker_html

    ticker = _ticker(symbol)
    ticker_html = format_ticker_html(ticker, symbol_suffix="")
    links = format_links_line(ticker)
    lines = [f"<b>🛡️ Risiko-Prüfung — 🔺 Cover {ticker_html}</b>"]
    if links:
        lines.append(links)
    share = f"Anteil <b>{pct * 100:.0f}%</b> des Shorts"
    if abs(pct - 1.0) < 1e-9:
        share += " <i>(Standard: alles)</i>"
    lines.extend([
        "",
        share,
        f"Kurs <b>{_format_price(price)}</b>",
        f"Menge <code>{amount:.4f}</code> {ticker} · ca. <b>${est_usdt:.0f}</b>",
        f"Geschätzter PnL <b>{est_pnl:+.2f}</b> USDT · Margin frei ≈ <b>${margin_freed:.0f}</b>",
    ])
    if total > 0:
        lines.append(
            f"Short <code>{total:.4f}</code> @ Entry {_format_price(entry)} · {lev:g}×"
        )
    lines.extend(_portfolio_block(status))
    lines.extend(_hint_block(decision, status, kind="cover"))
    lines.append("")
    lines.append("<b>Cover ausführen?</b>")
    return "\n".join(lines)


def _portfolio_block(status: dict) -> list[str]:
    throttle = "aktiv (Größe halbiert bei Auto-Trades)" if status.get("drawdown_throttle_active") else "aus"
    return [
        "",
        "<b>Portfolio</b>",
        f"· Cash <b>${status.get('virtual_balance', 0):,.0f}</b>",
        f"· Offene Positionen <b>{status.get('open_positions', 0)}/{status.get('max_open_positions', 0)}</b>",
        f"· Käufe (24h) <b>{status.get('daily_buys', status.get('daily_trades', 0))}/{status.get('max_daily_buys', status.get('max_daily_trades', 0))}</b>",
        f"· Verkäufe (24h) <b>{status.get('daily_sells', 0)}/{status.get('max_daily_sells', 0) or '∞'}</b>",
        f"· Drawdown <b>{status.get('drawdown_pct', 0):.1f}%</b> · Throttle {throttle}",
        f"· Max. Coin-Anteil <b>{status.get('max_position_percent', 0):.0f}%</b>",
    ]


def _hint_block(decision, status: dict, *, kind: str) -> list[str]:
    lines = ["", "<b>Risk Manager</b>"]
    lines.append("· Trade-Cooldown: <i>entfällt bei manueller Order</i>")
    if kind == "buy":
        requested = float(decision.order.usdt_amount)
        max_pct = status.get("max_position_percent", 30)
        if decision.drawdown_pct >= float(status.get("drawdown_throttle_pct", 10) or 10):
            lines.append(f"· Drawdown {decision.drawdown_pct:.1f}% — Auto-Trades würden verkleinert")
        if decision.size_multiplier != 1.0:
            lines.append(f"· Dynamische Skalierung (nur Auto): ×{decision.size_multiplier:.2f}")
        lines.append(f"· Konzentrationslimit: max. {max_pct:.0f}% des Portfolios pro Coin")
        if requested < status.get("base_usdt_per_trade", 25):
            lines.append("· Manueller Betrag unter Standard-Tradegröße — wird trotzdem ausgeführt")
    elif kind == "short":
        lines.append("· Isolierter Paper-Short: Margin = Notional / Hebel, Hebel durch shorts.leverage_cap gekappt")
        lines.append("· Short-Margin wird gegen Cash und shorts.max_margin_pct geprüft")
    elif kind == "cover":
        lines.append("· Cover schließt den Short (ganz oder anteilig) und gibt Margin frei")
    else:
        lines.append("· Stop-Loss-Verkäufe des Bots umgehen Cooldown weiterhin separat")
    noise = ("Approved", "Sell approved")
    if kind in ("short", "cover"):
        noise = noise + ("ok",)  # RiskManager._evaluate_short_or_cover approves with "ok"
    if decision.message and decision.message not in noise:
        lines.append(f"· {decision.message}")
    return lines


_REJECTION_ACTION = {"buy": "Kauf", "sell": "Verkauf", "short": "Short", "cover": "Cover"}


def _format_rejection(kind: str, symbol: str, decision, status: dict) -> str:
    action = _REJECTION_ACTION.get(kind, "Verkauf")
    lines = [
        f"❌ <b>{action} blockiert — {_ticker(symbol)}</b>",
        "",
        f"<b>Grund:</b> {decision.message}",
        "",
        "<b>Portfolio</b>",
        f"· Offene Positionen {status.get('open_positions', 0)}/{status.get('max_open_positions', 0)}",
        f"· Käufe (24h) {status.get('daily_buys', status.get('daily_trades', 0))}/{status.get('max_daily_buys', status.get('max_daily_trades', 0))}",
        f"· Verkäufe (24h) {status.get('daily_sells', 0)}/{status.get('max_daily_sells', 0) or '∞'}",
    ]
    if decision.code == "trade_cooldown":
        lines.append("· Hinweis: Cooldown gilt nur für Auto-Trades, nicht für manuelle Orders.")
    return "\n".join(lines)


def _pending_payload(kind: str, *, symbol: str, timeframe: str, usdt: float = None, pct: float = None, signal: str = "") -> dict:
    return {
        "kind": kind,
        "symbol": symbol,
        "timeframe": timeframe,
        "usdt": usdt,
        "pct": pct,
        "signal": signal,
    }


def _store_pending(
    ledger: OrderService,
    order: TradeOrder,
    *,
    timeframe: str,
    decision,
    request_extra: dict,
) -> str:
    import uuid

    token = uuid.uuid4().hex[:10]
    ledger.create_from_request(
        order,
        timeframe=timeframe,
        status="pending_confirmation",
        request_extra=request_extra,
        risk=decision,
        telegram_token=token,
    )
    return token


def request_buy_confirmation(
    trading: TradingService,
    *,
    symbol: str,
    timeframe: str,
    price: float,
    usdt: float,
) -> bool:
    trading.refresh()
    order = TradeOrder(type="BUY", symbol=symbol, price=price, amount=0, usdt_amount=usdt, source="manual")
    decision = trading.evaluate_risk(order, timeframe, source="manual")
    status = trading.risk.status_summary(price)
    status["drawdown_throttle_pct"] = trading.config.risk_config.get("drawdown_throttle_pct", 10.0)

    if not decision.approved:
        ledger = OrderService()
        ledger.record_rejected(order, decision, timeframe=timeframe)
        send_telegram_message(_format_rejection("buy", symbol, decision, status))
        return True

    ledger = OrderService()
    order_id = _store_pending(
        ledger, order, timeframe=timeframe, decision=decision, request_extra={"usdt": usdt},
    )
    msg = _format_buy_preview(decision, status, symbol=symbol, price=price, requested_usdt=usdt)
    from notifications.coin_links import inline_link_buttons

    keyboard = [[
        {"text": t("manual_confirm"), "callback_data": f"manual_ok:{order_id}"},
        {"text": t("manual_abort"), "callback_data": f"manual_no:{order_id}"},
    ]]
    link_row = (inline_link_buttons(symbol) or [None])[0]
    if link_row:
        keyboard.append(link_row)
    send_telegram_buttons(msg, keyboard)
    return True


def request_sell_confirmation(
    trading: TradingService,
    *,
    symbol: str,
    timeframe: str,
    price: float,
    amount: float,
    pct: float,
) -> bool:
    trading.refresh()
    order = TradeOrder(type="SELL", symbol=symbol, price=price, amount=amount, signal="SELL", source="manual")
    decision = trading.evaluate_risk(order, timeframe, source="manual")
    status = trading.risk.status_summary(price)
    status["drawdown_throttle_pct"] = trading.config.risk_config.get("drawdown_throttle_pct", 10.0)

    if not decision.approved:
        ledger = OrderService()
        ledger.record_rejected(order, decision, timeframe=timeframe, request_extra={"pct": pct})
        send_telegram_message(_format_rejection("sell", symbol, decision, status))
        return True

    ledger = OrderService()
    order_id = _store_pending(
        ledger,
        order,
        timeframe=timeframe,
        decision=decision,
        request_extra={"pct": pct, "amount": amount},
    )
    msg = _format_sell_preview(
        decision, status, symbol=symbol, price=price, amount=amount, pct=pct, timeframe=timeframe,
    )
    from notifications.coin_links import inline_link_buttons

    keyboard = [[
        {"text": t("manual_confirm"), "callback_data": f"manual_ok:{order_id}"},
        {"text": t("manual_abort"), "callback_data": f"manual_no:{order_id}"},
    ]]
    link_row = (inline_link_buttons(symbol) or [None])[0]
    if link_row:
        keyboard.append(link_row)
    send_telegram_buttons(msg, keyboard)
    return True


def _send_confirm_keyboard(msg: str, symbol: str, order_id: str) -> None:
    from notifications.coin_links import inline_link_buttons

    keyboard = [[
        {"text": t("manual_confirm"), "callback_data": f"manual_ok:{order_id}"},
        {"text": t("manual_abort"), "callback_data": f"manual_no:{order_id}"},
    ]]
    link_row = (inline_link_buttons(symbol) or [None])[0]
    if link_row:
        keyboard.append(link_row)
    send_telegram_buttons(msg, keyboard)


def request_short_confirmation(
    trading: TradingService,
    *,
    symbol: str,
    timeframe: str,
    price: float,
    usdt: Optional[float],
    leverage: Optional[float],
) -> bool:
    """Preview + confirm for ``/short``. ``usdt``/``leverage`` ``None`` means
    "operator gave no value" — the RiskManager default is resolved here and
    shown on the preview; the confirm tap executes exactly that size (#452)."""
    trading.refresh()
    order = TradeOrder(
        type="SHORT",
        symbol=symbol,
        price=price,
        amount=0,
        usdt_amount=float(usdt) if usdt else 0,
        signal="SHORT",
        source="manual",
        leverage=leverage,
    )
    decision = trading.evaluate_risk(order, timeframe, source="manual")
    status = trading.risk.status_summary(price)
    status["drawdown_throttle_pct"] = trading.config.risk_config.get("drawdown_throttle_pct", 10.0)

    if not decision.approved or decision.order is None:
        ledger = OrderService()
        ledger.record_rejected(
            order, decision, timeframe=timeframe,
            request_extra={"usdt": usdt, "leverage": leverage},
        )
        send_telegram_message(_format_rejection("short", symbol, decision, status))
        return True

    approved_usdt = float(decision.order.usdt_amount or 0)
    approved_lev = float(decision.order.leverage or 0) or None
    # Persist the *previewed* size, not the bare request: a confirm tap must
    # open what the operator saw, even if the config default changed meanwhile.
    previewed = TradeOrder(
        type="SHORT",
        symbol=symbol,
        price=price,
        amount=0,
        usdt_amount=approved_usdt,
        signal="SHORT",
        source="manual",
        leverage=approved_lev,
    )
    ledger = OrderService()
    order_id = _store_pending(
        ledger,
        previewed,
        timeframe=timeframe,
        decision=decision,
        request_extra={"requested_usdt": usdt, "requested_leverage": leverage},
    )
    msg = _format_short_preview(
        decision, status,
        symbol=symbol, price=price,
        requested_usdt=usdt, requested_leverage=leverage,
        timeframe=timeframe,
    )
    _send_confirm_keyboard(msg, symbol, order_id)
    return True


def request_cover_confirmation(
    trading: TradingService,
    *,
    symbol: str,
    timeframe: str,
    price: float,
    amount: float,
    pct: float,
) -> bool:
    """Preview + confirm for ``/cover``. ``pct`` is a fraction (1.0 = full)."""
    trading.refresh()
    order = TradeOrder(
        type="COVER", symbol=symbol, price=price, amount=amount, signal="COVER", source="manual",
    )
    decision = trading.evaluate_risk(order, timeframe, source="manual")
    status = trading.risk.status_summary(price)
    status["drawdown_throttle_pct"] = trading.config.risk_config.get("drawdown_throttle_pct", 10.0)

    if not decision.approved:
        ledger = OrderService()
        ledger.record_rejected(order, decision, timeframe=timeframe, request_extra={"pct": pct})
        send_telegram_message(_format_rejection("cover", symbol, decision, status))
        return True

    ledger = OrderService()
    order_id = _store_pending(
        ledger,
        order,
        timeframe=timeframe,
        decision=decision,
        request_extra={"pct": pct, "amount": amount},
    )
    msg = _format_cover_preview(
        decision, status, symbol=symbol, price=price, amount=amount, pct=pct, timeframe=timeframe,
    )
    _send_confirm_keyboard(msg, symbol, order_id)
    return True


def _pending_from_record(record: dict) -> Optional[dict]:
    req = record.get("request", {})
    side = (record.get("side") or "").lower()
    if side == "short":
        return {
            "kind": "short",
            "symbol": record.get("symbol"),
            "timeframe": record.get("timeframe", "4h"),
            "usdt": req.get("usdt"),
            "leverage": req.get("leverage"),
            "pct": None,
            "signal": record.get("signal", "SHORT"),
        }
    if side == "cover":
        return {
            "kind": "cover",
            "symbol": record.get("symbol"),
            "timeframe": record.get("timeframe", "4h"),
            "usdt": None,
            "pct": req.get("pct"),
            "signal": record.get("signal", "COVER"),
        }
    if side == "buy":
        return {
            "kind": "buy",
            "symbol": record.get("symbol"),
            "timeframe": record.get("timeframe", "4h"),
            "usdt": req.get("usdt"),
            "pct": None,
            "signal": "",
        }
    if side == "sell":
        return {
            "kind": "sell",
            "symbol": record.get("symbol"),
            "timeframe": record.get("timeframe", "4h"),
            "usdt": None,
            "pct": req.get("pct"),
            "signal": record.get("signal", "SELL"),
        }
    return None


def _execute_pending(order_id: str, trading: TradingService) -> None:
    ledger = OrderService()
    record = ledger.get_by_id(order_id)
    if not record or record.get("status") != "pending_confirmation":
        send_telegram_message(t("manual_expired"))
        return

    pending = _pending_from_record(record)
    if not pending:
        send_telegram_message(t("manual_invalid"))
        return

    trading.refresh()
    symbol = pending["symbol"]
    timeframe = pending["timeframe"]
    from price_fetcher import get_prices

    price = get_prices(symbol)[0]
    if not price or price <= 0:
        ledger.update_status(order_id, "failed", error="Price unavailable")
        send_telegram_message(t("manual_price_gone", sym=_ticker(symbol)))
        return

    kind = pending["kind"]
    if kind == "buy":
        result = trading.execute_buy(symbol, timeframe, price, pending["usdt"], order_id=order_id)
    elif kind == "short":
        usdt = float(pending.get("usdt") or 0)
        if usdt <= 0:
            ledger.update_status(order_id, "failed", error="No short size")
            send_telegram_message(t("manual_invalid"))
            return
        lev = pending.get("leverage")
        result = trading.execute_short(
            symbol, timeframe, price,
            usdt=usdt, leverage=float(lev) if lev else None, order_id=order_id,
        )
    elif kind == "cover":
        pos = get_position(symbol, timeframe)
        if not is_short(pos):
            ledger.update_status(order_id, "failed", error="No short to cover")
            send_telegram_message(t("manual_no_short", sym=_ticker(symbol)))
            return
        amount = float(pos.get("amount", 0)) * float(pending["pct"] or 0)
        if amount <= 0:
            ledger.update_status(order_id, "failed", error="No coverable amount")
            send_telegram_message(t("manual_no_short", sym=_ticker(symbol)))
            return
        result = trading.execute_cover(symbol, timeframe, price, amount=amount, order_id=order_id)
    else:
        pos = get_position(symbol, timeframe)
        amount = float(pos.get("amount", 0)) * float(pending["pct"])
        if amount <= 0:
            ledger.update_status(order_id, "failed", error="No sellable amount")
            send_telegram_message(t("manual_no_qty", sym=_ticker(symbol)))
            return
        result = trading.execute_sell(symbol, timeframe, price, pending["signal"], amount, order_id=order_id)

    if not result.executed:
        send_telegram_message(t("manual_failed", msg=result.message))


def handle_callback(callback_query: dict) -> bool:
    data = callback_query.get("data", "")
    if not data.startswith("manual_"):
        return False

    answer_callback_query(callback_query.get("id"))
    parts = data.split(":", 1)
    if len(parts) != 2:
        return True

    action, order_id = parts
    trading = TradingService()
    ledger = OrderService()

    if action == "manual_no":
        record = ledger.get_by_id(order_id)
        if record and record.get("status") == "pending_confirmation":
            ledger.update_status(order_id, "cancelled")
            send_telegram_message(t("manual_cancelled"))
        else:
            send_telegram_message(t("manual_unknown"))
        return True

    if action == "manual_ok":
        _execute_pending(order_id, trading)
        return True

    return False