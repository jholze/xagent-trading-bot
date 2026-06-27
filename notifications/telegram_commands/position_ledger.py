"""Per-position trade tree with FIFO open PnL and realized sell PnL."""

from __future__ import annotations

from dataclasses import dataclass, field

from data_manager import load_orders
from services.order_service import source_label


@dataclass
class _Lot:
    qty: float
    price: float
    usdt: float
    signal: str
    source: str
    ts: str
    remaining: float


@dataclass
class _Event:
    kind: str
    label: str
    ts: str
    usdt: float
    price: float
    source: str
    signal: str
    realized_usd: float | None = None
    open_qty: float = 0.0
    open_pct: float = 0.0
    open_usd: float = 0.0
    lot: _Lot | None = field(default=None, repr=False)


def _order_usdt(order: dict) -> float:
    for section in (order.get("execution") or {}, order.get("request") or {}):
        raw = section.get("usdt")
        if raw is not None:
            try:
                val = float(raw)
                if val > 0:
                    return val
            except (TypeError, ValueError):
                pass
    price = float((order.get("execution") or {}).get("price") or (order.get("request") or {}).get("price") or 0)
    amount = float((order.get("execution") or {}).get("amount") or (order.get("request") or {}).get("amount") or 0)
    if price > 0 and amount > 0:
        return price * amount
    return 0.0


def _order_ts(order: dict) -> str:
    return (
        (order.get("timestamps") or {}).get("filled")
        or (order.get("timestamps") or {}).get("created")
        or ""
    )


def _fmt_ts_short(iso_ts: str) -> str:
    if not iso_ts or len(iso_ts) < 16:
        return iso_ts[:10] if iso_ts else ""
    return iso_ts[5:10].replace("-", ".") + " " + iso_ts[11:16]


def _sell_label(signal: str) -> str:
    sig = (signal or "").upper()
    if "STOP" in sig and "FULL" in sig:
        return "Stop Voll"
    if "STOP" in sig:
        return "Stop 50%"
    if "30" in sig:
        return "Verkauf 30%"
    if "20" in sig:
        return "Verkauf 20%"
    if "10" in sig:
        return "Verkauf 10%"
    if "50" in sig or "PARTIAL" in sig:
        return "Verkauf 50%"
    if sig:
        return sig.replace("_", " ")
    return "Verkauf"


def _buy_label(signal: str, dca_index: int) -> str:
    sig = (signal or "").upper()
    if sig == "BUY_DCA" or "DCA" in sig:
        return f"DCA #{dca_index}"
    return "Entry"


def _event_icon(kind: str, label: str) -> str:
    if kind == "sell":
        return "🔴"
    if label.startswith("DCA"):
        return "🔵"
    return "🟢"


def orders_by_position_key(scope: str) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for order in load_orders(scope).get("orders", []):
        if order.get("status") != "filled":
            continue
        symbol = order.get("symbol", "")
        tf = order.get("timeframe") or "4h"
        key = f"{symbol}|{tf}"
        grouped.setdefault(key, []).append(order)
    for orders in grouped.values():
        orders.sort(key=_order_ts)
    return grouped


def orders_for_position(symbol: str, timeframe: str, scope: str) -> list[dict]:
    orders = [
        o
        for o in load_orders(scope).get("orders", [])
        if o.get("status") == "filled"
        and o.get("symbol") == symbol
        and (o.get("timeframe") or "4h") == timeframe
    ]
    orders.sort(key=_order_ts)
    return orders


def replay_position_events(orders: list[dict], *, mark_price: float) -> list[dict]:
    lots: list[_Lot] = []
    events: list[_Event] = []
    dca_count = 0

    for order in orders:
        side = (order.get("side") or "").lower()
        price = float((order.get("execution") or {}).get("price") or (order.get("request") or {}).get("price") or 0)
        amount = float((order.get("execution") or {}).get("amount") or (order.get("request") or {}).get("amount") or 0)
        usdt = _order_usdt(order)
        ts = _order_ts(order)
        signal = (order.get("signal") or "").strip()
        source = source_label(order.get("source", "auto"))

        if side == "buy" and amount > 0 and price > 0:
            if signal.upper() == "BUY_DCA" or "DCA" in signal.upper():
                dca_count += 1
                label = _buy_label(signal, dca_count)
            else:
                label = _buy_label(signal, dca_count)
            lot = _Lot(qty=amount, price=price, usdt=usdt, signal=signal, source=source, ts=ts, remaining=amount)
            lots.append(lot)
            events.append(
                _Event(
                    kind="buy",
                    label=label,
                    ts=ts,
                    usdt=usdt,
                    price=price,
                    source=source,
                    signal=signal,
                    lot=lot,
                )
            )
        elif side == "sell" and amount > 0:
            sell_qty = amount
            for lot in lots:
                if sell_qty <= 1e-12:
                    break
                if lot.remaining <= 1e-12:
                    continue
                take = min(sell_qty, lot.remaining)
                lot.remaining -= take
                sell_qty -= take
            pnl = order.get("pnl")
            realized = float(pnl) if pnl is not None else None
            events.append(
                _Event(
                    kind="sell",
                    label=_sell_label(signal),
                    ts=ts,
                    usdt=usdt,
                    price=price,
                    source=source,
                    signal=signal,
                    realized_usd=realized,
                )
            )

    mark = float(mark_price or 0)
    out: list[dict] = []
    for ev in events:
        row = {
            "kind": "sell" if ev.kind == "sell" else "entry",
            "label": ev.label,
            "ts": ev.ts,
            "usdt": ev.usdt,
            "price": ev.price,
            "source": ev.source,
            "signal": ev.signal,
        }
        if ev.kind == "sell":
            row["realized_usd"] = ev.realized_usd if ev.realized_usd is not None else 0.0
        elif ev.lot and mark > 0 and ev.lot.remaining > 1e-12:
            row["open_qty"] = ev.lot.remaining
            row["open_pct"] = (mark / ev.lot.price - 1) * 100 if ev.lot.price > 0 else 0.0
            row["open_usd"] = (mark - ev.lot.price) * ev.lot.remaining
        else:
            row["open_qty"] = 0.0
            row["open_pct"] = 0.0
            row["open_usd"] = 0.0
        if ev.kind == "buy" and row["open_qty"] <= 1e-12:
            row["closed"] = True
        out.append(row)
    return out


def _branch_char(index: int, total: int) -> str:
    return "└─" if index == total - 1 else "├─"


def format_event_line(ev: dict) -> str:
    from price_fetcher import format_usdt_price

    ts = _fmt_ts_short(ev.get("ts", ""))
    usdt = float(ev.get("usdt", 0) or 0)
    price = format_usdt_price(float(ev.get("price", 0) or 0))
    source = ev.get("source", "Auto")
    label = ev.get("label", "")
    icon = _event_icon(ev.get("kind", ""), label)

    if ev.get("kind") == "sell":
        realized = float(ev.get("realized_usd", 0) or 0)
        return (
            f"{icon} {label} · <b>${usdt:,.0f}</b> @{price} · {ts} · {source} · "
            f"real <b>${realized:+.0f}</b>"
        )

    if ev.get("closed"):
        return (
            f"{icon} {label} · <b>${usdt:,.0f}</b> @{price} · {ts} · {source} · "
            f"<i>geschlossen</i>"
        )
    open_pct = float(ev.get("open_pct", 0) or 0)
    open_usd = float(ev.get("open_usd", 0) or 0)
    return (
        f"{icon} {label} · <b>${usdt:,.0f}</b> @{price} · {ts} · {source} · "
        f"offen <code>{open_pct:+.1f}%</code> (<b>${open_usd:+.0f}</b>)"
    )


def format_gesamt_line(
    *,
    value_usdt: float,
    unreal_usd: float,
    realized_usd: float,
) -> str:
    total = unreal_usd + realized_usd
    return (
        f"Gesamt · Wert <b>${value_usdt:,.0f}</b> · Unreal <b>${unreal_usd:+.0f}</b> · "
        f"Real <b>${realized_usd:+.0f}</b> · Σ <b>${total:+.0f}</b>"
    )


def build_position_trade_tree(
    position: dict,
    *,
    mark_price: float,
    orders: list[dict] | None = None,
    scope: str | None = None,
    max_events: int = 6,
) -> list[str]:
    symbol = position.get("symbol", "")
    tf = position.get("timeframe", "4h")
    if orders is None:
        from data_manager import resolve_ledger_scope

        orders = orders_for_position(symbol, tf, scope or resolve_ledger_scope())

    amount = float(position.get("amount", 0) or 0)
    entry = float(position.get("average_entry", position.get("entry_price", 0)) or 0)
    mark = float(mark_price or 0)
    unreal = (mark - entry) * amount if entry > 0 and mark > 0 else 0.0
    value_usdt = mark * amount if mark > 0 else 0.0
    realized = float(position.get("realized_pnl", 0) or 0)

    events = replay_position_events(orders, mark_price=mark)
    hidden = max(0, len(events) - max_events)
    visible = events[-max_events:] if hidden else events

    lines: list[str] = []
    total_branches = 1 + len(visible) + (1 if hidden else 0)
    idx = 0
    lines.append(f"   {_branch_char(idx, total_branches)} {format_gesamt_line(
        value_usdt=value_usdt, unreal_usd=unreal, realized_usd=realized,
    )}")
    idx += 1

    for i, ev in enumerate(visible):
        branch = _branch_char(idx, total_branches)
        idx += 1
        lines.append(f"   {branch} {format_event_line(ev)}")

    if hidden:
        lines.append(f"   └─ … +{hidden} ältere Transaktionen")

    return lines