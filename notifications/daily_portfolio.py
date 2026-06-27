"""Daily NAV and trade stats for cycle summary and /positions."""

from __future__ import annotations

import time
from datetime import date, datetime

from core.config import get_bot_config
from core.portfolio_baseline import initial_capital
from data_manager import (
    compute_sim_cash_from_orders,
    compute_sim_cash_from_trades,
    compute_sim_realized_pnl,
    load_live_trade_history,
    load_orders,
    load_trade_history,
    resolve_ledger_scope,
    uses_exchange_ledger,
)
from notifications.telegram_commands.position_display import (
    _position_metrics,
    build_price_fallbacks,
    position_symbol,
)
from price_fetcher import get_prices_batch
from strategies.positions import get_key, list_active_positions


def _history_and_trades(trading_mode: str = None):
    cfg = get_bot_config()
    mode = trading_mode or cfg.trading_mode
    if uses_exchange_ledger(mode):
        history = load_live_trade_history()
    else:
        history = load_trade_history()
    return history, history.get("trades", [])


def _position_value_from_snapshot(snapshot: dict, prices: dict) -> float:
    total = 0.0
    for key, pos in snapshot.items():
        amt = float(pos.get("amount", 0) or 0)
        if amt <= 1e-12:
            continue
        sym = key.rpartition("_")[0].replace("_", "/")
        price = float(prices.get(sym, 0) or 0)
        total += price * amt
    return total


def _filled_orders_before(cutoff_iso: str, scope: str) -> list:
    orders = [
        o
        for o in load_orders(scope).get("orders", [])
        if o.get("status") == "filled"
        and ((o.get("timestamps") or {}).get("filled") or "") < cutoff_iso
    ]
    orders.sort(
        key=lambda o: (o.get("timestamps") or {}).get("filled")
        or (o.get("timestamps") or {}).get("created")
        or ""
    )
    return orders


def _cash_at_cutoff(
    cutoff_iso: str,
    scope: str,
    initial: float,
    pre_trades: list,
) -> float:
    """Derive opening cash from orders when available (matches position replay)."""
    pre_orders = _filled_orders_before(cutoff_iso, scope)
    if pre_orders:
        return compute_sim_cash_from_orders(pre_orders, initial)
    return compute_sim_cash_from_trades(pre_trades, initial)


def _snapshot_from_orders_before(cutoff_iso: str, scope: str) -> dict:
    orders = _filled_orders_before(cutoff_iso, scope)
    snapshot: dict = {}
    for order in orders:
        symbol = order.get("symbol", "")
        tf = order.get("timeframe", "4h")
        if not symbol:
            continue
        key = get_key(symbol, tf)
        pos = snapshot.setdefault(
            key,
            {"amount": 0.0, "peak_amount": 0.0, "average_entry": 0.0, "sold_percent": 0.0},
        )
        execution = order.get("execution") or {}
        request = order.get("request") or {}
        price = float(execution.get("price") or request.get("price") or 0)
        amount = float(execution.get("amount") or request.get("amount") or 0)
        side = (order.get("side") or "").lower()
        if side == "buy" and amount > 0 and price > 0:
            old = pos["amount"]
            new = old + amount
            if old > 0:
                pos["average_entry"] = (pos["average_entry"] * old + price * amount) / new
            else:
                pos["average_entry"] = price
            pos["amount"] = new
            pos["peak_amount"] = new
            pos["sold_percent"] = 0.0
        elif side == "sell" and amount > 0:
            original = pos["amount"]
            sell_amount = min(amount, original) if original > 0 else amount
            pos["amount"] = max(0.0, original - sell_amount)
            peak = float(pos.get("peak_amount") or original or 0)
            if peak > 0:
                pos["sold_percent"] = min(1.0, max(0.0, 1.0 - pos["amount"] / peak))
    return snapshot


def trades_today(trades: list = None) -> list:
    trades = trades if trades is not None else _history_and_trades()[1]
    today = date.today().isoformat()
    return [t for t in trades if str(t.get("timestamp", "")).startswith(today)]


def realized_pnl_for(trade_list: list) -> float:
    return round(
        sum(float(t.get("pnl") or 0) for t in trade_list if t.get("type") == "SELL"),
        2,
    )


_nav_start_cache: dict[str, tuple[float, float]] = {}
_NAV_CACHE_TTL = 120.0


def estimate_nav_at_day_start(
    trading_mode: str = None,
    *,
    prices: dict | None = None,
    cache_ttl_sec: float = _NAV_CACHE_TTL,
) -> float:
    """Replay ledger at today's first trade; mark open lots with current prices."""
    cfg = get_bot_config()
    mode = trading_mode or cfg.trading_mode
    scope = resolve_ledger_scope(mode)
    cache_key = f"{date.today().isoformat()}:{scope}"
    now = time.time()
    cached = _nav_start_cache.get(cache_key)
    if cached and now - cached[0] < max(5.0, float(cache_ttl_sec)):
        return cached[1]

    today_trades = trades_today()
    if today_trades:
        cutoff = min(t.get("timestamp", "") for t in today_trades)
    else:
        cutoff = f"{date.today().isoformat()}T23:59:59"
    history, all_trades = _history_and_trades(mode)
    initial = initial_capital(
        scope=scope,
        config=cfg.raw,
        history=history,
        trading_mode=mode,
    )
    pre = [t for t in all_trades if (t.get("timestamp") or "") < cutoff]
    cash = _cash_at_cutoff(cutoff, scope, initial, pre)
    snap = _snapshot_from_orders_before(cutoff, scope)
    symbols = sorted(
        {key.rpartition("_")[0].replace("_", "/") for key in snap if snap[key].get("amount", 0) > 1e-12}
    )
    if symbols:
        if prices and all(float(prices.get(s, 0) or 0) > 0 for s in symbols):
            price_map = prices
        else:
            price_map = get_prices_batch(symbols)
    else:
        price_map = {}
    nav = cash + _position_value_from_snapshot(snap, price_map)
    _nav_start_cache[cache_key] = (now, nav)
    return nav


def format_daily_nav_line(
    trading_mode: str = None,
    total_value: float = None,
    *,
    prices: dict | None = None,
    cache_ttl_sec: float = _NAV_CACHE_TTL,
) -> str:
    """One-line daily stats for cycle summary."""
    mode = trading_mode or get_bot_config().trading_mode
    history, trades = _history_and_trades(mode)
    today = trades_today(trades)
    if not today:
        return ""

    realized_today = realized_pnl_for(today)
    buys = sum(1 for t in today for _ in [0] if t.get("type") == "BUY")
    sells = sum(1 for t in today for _ in [0] if t.get("type") == "SELL")
    nav_start = estimate_nav_at_day_start(mode, prices=prices, cache_ttl_sec=cache_ttl_sec)
    if total_value is None:
        from notifications.terminal_dashboard import _portfolio_snapshot

        total_value = float(_portfolio_snapshot(mode).get("total_value", 0) or 0)
    nav_delta = total_value - nav_start
    sign = "+" if nav_delta >= 0 else ""
    pnl_sign = "+" if realized_today >= 0 else ""
    return (
        f"📅 <b>Heute:</b> {buys} Käufe / {sells} Verkäufe · "
        f"Real. <b>{pnl_sign}${realized_today:,.0f}</b> · "
        f"NAV <b>${total_value:,.0f}</b> ({sign}${nav_delta:,.0f} vs. Tagesstart ${nav_start:,.0f})"
    )