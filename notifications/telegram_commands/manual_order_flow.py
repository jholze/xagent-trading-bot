"""Manual /buy and /sell confirmation flow with risk preview."""

from __future__ import annotations

from typing import Optional

from core.models import TradeOrder
from services.order_service import OrderService
from services.trading_service import TradingService
from strategies.positions import get_position
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


def _portfolio_block(status: dict) -> list[str]:
    throttle = "aktiv (Größe halbiert bei Auto-Trades)" if status.get("drawdown_throttle_active") else "aus"
    return [
        "",
        "<b>Portfolio</b>",
        f"· Cash <b>${status.get('virtual_balance', 0):,.0f}</b>",
        f"· Offene Positionen <b>{status.get('open_positions', 0)}/{status.get('max_open_positions', 0)}</b>",
        f"· Trades (24h) <b>{status.get('daily_trades', 0)}/{status.get('max_daily_trades', 0)}</b>",
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
    else:
        lines.append("· Stop-Loss-Verkäufe des Bots umgehen Cooldown weiterhin separat")
    if decision.message and decision.message not in ("Approved", "Sell approved"):
        lines.append(f"· {decision.message}")
    return lines


def _format_rejection(kind: str, symbol: str, decision, status: dict) -> str:
    action = "Kauf" if kind == "buy" else "Verkauf"
    lines = [
        f"❌ <b>{action} blockiert — {_ticker(symbol)}</b>",
        "",
        f"<b>Grund:</b> {decision.message}",
        "",
        "<b>Portfolio</b>",
        f"· Offene Positionen {status.get('open_positions', 0)}/{status.get('max_open_positions', 0)}",
        f"· Trades (24h) {status.get('daily_trades', 0)}/{status.get('max_daily_trades', 0)}",
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
        {"text": "✅ Bestätigen", "callback_data": f"manual_ok:{order_id}"},
        {"text": "❌ Abbrechen", "callback_data": f"manual_no:{order_id}"},
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
        {"text": "✅ Bestätigen", "callback_data": f"manual_ok:{order_id}"},
        {"text": "❌ Abbrechen", "callback_data": f"manual_no:{order_id}"},
    ]]
    link_row = (inline_link_buttons(symbol) or [None])[0]
    if link_row:
        keyboard.append(link_row)
    send_telegram_buttons(msg, keyboard)
    return True


def _pending_from_record(record: dict) -> Optional[dict]:
    req = record.get("request", {})
    side = (record.get("side") or "").lower()
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
        send_telegram_message("⏱️ Diese Order ist abgelaufen. Bitte <code>/buy</code> oder <code>/sell</code> erneut senden.")
        return

    pending = _pending_from_record(record)
    if not pending:
        send_telegram_message("❌ Ungültige Order-Daten.")
        return

    trading.refresh()
    symbol = pending["symbol"]
    timeframe = pending["timeframe"]
    from price_fetcher import get_prices

    price = get_prices(symbol)[0]
    if not price or price <= 0:
        ledger.update_status(order_id, "failed", error="Price unavailable")
        send_telegram_message(f"❌ Kurs für {_ticker(symbol)} nicht verfügbar. Order abgebrochen.")
        return

    if pending["kind"] == "buy":
        result = trading.execute_buy(symbol, timeframe, price, pending["usdt"], order_id=order_id)
    else:
        pos = get_position(symbol, timeframe)
        amount = float(pos.get("amount", 0)) * float(pending["pct"])
        if amount <= 0:
            ledger.update_status(order_id, "failed", error="No sellable amount")
            send_telegram_message(f"❌ Keine verkaufbare Menge für {_ticker(symbol)}.")
            return
        result = trading.execute_sell(symbol, timeframe, price, pending["signal"], amount, order_id=order_id)

    if not result.executed:
        send_telegram_message(f"❌ Order fehlgeschlagen: {result.message}")


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
            send_telegram_message("🚫 Manuelle Order abgebrochen.")
        else:
            send_telegram_message("⏱️ Order bereits abgelaufen oder unbekannt.")
        return True

    if action == "manual_ok":
        _execute_pending(order_id, trading)
        return True

    return False