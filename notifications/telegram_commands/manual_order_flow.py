"""Manual /buy and /sell confirmation flow with risk preview."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Optional

from core.models import TradeOrder
from services.trading_service import TradingService
from strategies.positions import get_position
from telegram_notifier import answer_callback_query, send_telegram_buttons, send_telegram_message

_PENDING: dict[str, dict] = {}
_TTL = timedelta(minutes=10)


def _cleanup_expired() -> None:
    cutoff = datetime.now() - _TTL
    for key in list(_PENDING.keys()):
        if _PENDING[key]["created"] < cutoff:
            del _PENDING[key]


def _ticker(symbol: str) -> str:
    return symbol.replace("/USDT", "").split("/")[0]


def _store_pending(payload: dict) -> str:
    _cleanup_expired()
    order_id = uuid.uuid4().hex[:10]
    payload["created"] = datetime.now()
    _PENDING[order_id] = payload
    return order_id


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
    lines = [
        f"<b>🛡️ Risiko-Prüfung — Kauf {_ticker(symbol)}</b>",
        "",
        f"Kurs <b>${price:.4f}</b>",
        f"Angefragt <b>${requested_usdt:.0f}</b> USDT",
    ]
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
    lines = [
        f"<b>🛡️ Risiko-Prüfung — Verkauf {_ticker(symbol)}</b>",
        "",
        f"Anteil <b>{pct * 100:.0f}%</b> der Position",
        f"Kurs <b>${price:.4f}</b>",
        f"Menge <code>{amount:.4f}</code> {_ticker(symbol)} · ca. <b>${est_usdt:.0f}</b>",
    ]
    if total > 0:
        lines.append(f"Position <code>{total:.4f}</code> @ Entry ${entry:.4f}")
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


def request_buy_confirmation(
    trading: TradingService,
    *,
    symbol: str,
    timeframe: str,
    price: float,
    usdt: float,
) -> bool:
    trading.refresh()
    order = TradeOrder(type="BUY", symbol=symbol, price=price, amount=0, usdt_amount=usdt)
    decision = trading.evaluate_risk(order, timeframe, source="manual")
    status = trading.risk.status_summary(price)
    status["drawdown_throttle_pct"] = trading.config.risk_config.get("drawdown_throttle_pct", 10.0)

    if not decision.approved:
        send_telegram_message(_format_rejection("buy", symbol, decision, status))
        return True

    order_id = _store_pending({
        "kind": "buy",
        "symbol": symbol,
        "timeframe": timeframe,
        "usdt": usdt,
        "pct": None,
        "signal": "",
    })
    msg = _format_buy_preview(decision, status, symbol=symbol, price=price, requested_usdt=usdt)
    send_telegram_buttons(msg, [[
        {"text": "✅ Bestätigen", "callback_data": f"manual_ok:{order_id}"},
        {"text": "❌ Abbrechen", "callback_data": f"manual_no:{order_id}"},
    ]])
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
    order = TradeOrder(type="SELL", symbol=symbol, price=price, amount=amount, signal="SELL")
    decision = trading.evaluate_risk(order, timeframe, source="manual")
    status = trading.risk.status_summary(price)
    status["drawdown_throttle_pct"] = trading.config.risk_config.get("drawdown_throttle_pct", 10.0)

    if not decision.approved:
        send_telegram_message(_format_rejection("sell", symbol, decision, status))
        return True

    order_id = _store_pending({
        "kind": "sell",
        "symbol": symbol,
        "timeframe": timeframe,
        "usdt": None,
        "pct": pct,
        "signal": "SELL",
    })
    msg = _format_sell_preview(
        decision, status, symbol=symbol, price=price, amount=amount, pct=pct, timeframe=timeframe,
    )
    send_telegram_buttons(msg, [[
        {"text": "✅ Bestätigen", "callback_data": f"manual_ok:{order_id}"},
        {"text": "❌ Abbrechen", "callback_data": f"manual_no:{order_id}"},
    ]])
    return True


def _execute_pending(order_id: str, trading: TradingService) -> None:
    pending = _PENDING.pop(order_id, None)
    if not pending:
        send_telegram_message("⏱️ Diese Order ist abgelaufen. Bitte <code>/buy</code> oder <code>/sell</code> erneut senden.")
        return

    trading.refresh()
    symbol = pending["symbol"]
    timeframe = pending["timeframe"]
    from price_fetcher import get_prices

    price = get_prices(symbol)[0]
    if not price or price <= 0:
        send_telegram_message(f"❌ Kurs für {_ticker(symbol)} nicht verfügbar. Order abgebrochen.")
        return

    if pending["kind"] == "buy":
        result = trading.execute_buy(symbol, timeframe, price, pending["usdt"])
    else:
        pos = get_position(symbol, timeframe)
        amount = float(pos.get("amount", 0)) * float(pending["pct"])
        if amount <= 0:
            send_telegram_message(f"❌ Keine verkaufbare Menge für {_ticker(symbol)}.")
            return
        result = trading.execute_sell(symbol, timeframe, price, pending["signal"], amount)

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

    if action == "manual_no":
        if _PENDING.pop(order_id, None):
            send_telegram_message("🚫 Manuelle Order abgebrochen.")
        else:
            send_telegram_message("⏱️ Order bereits abgelaufen oder unbekannt.")
        return True

    if action == "manual_ok":
        _execute_pending(order_id, trading)
        return True

    return False