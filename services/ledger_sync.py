"""Isolate position state per ledger scope (demo / paper / live)."""

from __future__ import annotations

import os
import shutil

from logger import log

LEGACY_POSITIONS_FILE = "positions.json"


def _scope_for_trading_mode(trading_mode: str) -> str:
    from data_manager import is_demo_mode, resolve_ledger_scope

    if is_demo_mode():
        return "demo"
    return resolve_ledger_scope(trading_mode)


def migrate_legacy_positions() -> None:
    """Copy legacy positions.json into positions.paper.json once (production only)."""
    from data_manager import is_demo_mode, resolve_positions_file

    if is_demo_mode():
        return

    paper_path = resolve_positions_file("paper")
    if os.path.exists(paper_path):
        return
    if not os.path.exists(LEGACY_POSITIONS_FILE):
        return
    try:
        shutil.copy2(LEGACY_POSITIONS_FILE, paper_path)
        log(f"Migrated {LEGACY_POSITIONS_FILE} → {paper_path}", "INFO")
    except Exception as e:
        log(f"Legacy positions migration failed: {e}", "WARNING")


def _build_positions_snapshot_from_orders(scope: str) -> dict:
    """Derive position state from filled orders for *scope*."""
    from data_manager import load_orders
    from strategies.positions import get_key

    orders = [
        o
        for o in load_orders(scope).get("orders", [])
        if o.get("status") == "filled"
    ]
    orders.sort(
        key=lambda o: (
            o.get("timestamps", {}).get("filled")
            or o.get("timestamps", {}).get("created")
            or ""
        )
    )

    snapshot: dict = {}
    for order in orders:
        symbol = order.get("symbol", "")
        timeframe = order.get("timeframe", "4h")
        if not symbol:
            continue
        key = get_key(symbol, timeframe)
        pos = snapshot.setdefault(
            key,
            {
                "amount": 0.0,
                "peak_amount": 0.0,
                "sold_percent": 0.0,
                "average_entry": 0.0,
                "realized_pnl": 0.0,
                "last_buy_price": 0.0,
                "last_ampel": "🟡",
                "last_rsi": 45.0,
                "last_action": None,
                "last_trade_at": None,
                "last_trade_type": None,
                "rsi_sell_tiers_done": {},
            },
        )

        side = (order.get("side") or "").lower()
        execution = order.get("execution") or {}
        request = order.get("request") or {}
        price = float(execution.get("price") or request.get("price") or 0)
        amount = float(execution.get("amount") or request.get("amount") or 0)
        trade_ts = (
            order.get("timestamps", {}).get("filled")
            or order.get("timestamps", {}).get("created")
        )

        if side == "buy" and amount > 0 and price > 0:
            old_amount = pos["amount"]
            new_amount = old_amount + amount
            if old_amount > 0:
                pos["average_entry"] = (
                    pos["average_entry"] * old_amount + price * amount
                ) / new_amount
            else:
                pos["average_entry"] = price
            pos["amount"] = new_amount
            pos["peak_amount"] = new_amount
            pos["sold_percent"] = 0.0
            pos["last_buy_price"] = price
            pos["last_action"] = "BUY"
            pos["last_trade_type"] = "BUY"
            pos["last_trade_at"] = trade_ts
            pos["rsi_sell_tiers_done"] = {}
        elif side == "sell" and amount > 0:
            original = pos["amount"]
            sell_amount = min(amount, original) if original > 0 else amount
            pos["amount"] = max(0.0, original - sell_amount)
            peak = float(pos.get("peak_amount") or original or 0)
            if peak > 0:
                pos["sold_percent"] = min(
                    1.0, max(0.0, 1.0 - pos["amount"] / peak)
                )
            pos["last_action"] = "SELL"
            pos["last_trade_type"] = "SELL"
            pos["last_trade_at"] = trade_ts
            pnl = order.get("pnl")
            if pnl is not None:
                pos["realized_pnl"] = float(pos.get("realized_pnl", 0)) + float(pnl)
    return snapshot


def count_open_positions_from_orders(scope: str) -> int:
    from strategies.positions import is_open_position

    snapshot = _build_positions_snapshot_from_orders(scope)
    return sum(1 for p in snapshot.values() if is_open_position(p))


def rebuild_positions_from_orders(scope: str) -> int:
    """Rebuild in-memory positions for *scope* from filled orders only."""
    from strategies.positions import apply_positions_snapshot, flush_positions, is_open_position

    snapshot = _build_positions_snapshot_from_orders(scope)
    from data_manager import load_orders

    orders = [o for o in load_orders(scope).get("orders", []) if o.get("status") == "filled"]

    apply_positions_snapshot(snapshot, scope=scope)
    flush_positions(scope, force=True)
    open_count = sum(1 for p in snapshot.values() if is_open_position(p))
    log(
        f"Rebuilt positions for scope={scope} from {len(orders)} filled order(s), "
        f"{open_count} open",
        "INFO",
    )
    return open_count


def activate_ledger_scope(scope: str, *, rebuild: bool = False) -> int:
    """Switch active in-memory positions to *scope*."""
    from strategies.positions import bootstrap_positions, count_open_positions

    migrate_legacy_positions()
    if rebuild:
        return rebuild_positions_from_orders(scope)
    bootstrap_positions(scope=scope)
    return count_open_positions()


def on_trading_mode_change(old_mode: str, new_mode: str) -> str:
    """Persist outgoing ledger and load the target ledger without cross-contamination."""
    from strategies.positions import count_open_positions, flush_positions, get_active_scope

    old_scope = _scope_for_trading_mode(old_mode)
    new_scope = _scope_for_trading_mode(new_mode)
    if old_scope == new_scope:
        return ""

    flush_positions(scope=old_scope, force=True)
    open_count = activate_ledger_scope(new_scope, rebuild=True)
    active = get_active_scope()
    if active != new_scope:
        log(f"Ledger scope mismatch after switch: {active} != {new_scope}", "WARNING")
    return (
        f"Ledger: <b>{old_scope.upper()}</b> → <b>{new_scope.upper()}</b>\n"
        f"Positionen aus Orders neu aufgebaut: <b>{open_count}</b> offen"
    )


def reconcile_peak_amounts(scope: str) -> bool:
    """Backfill peak_amount and sold_percent from filled orders for open lots."""
    from strategies.positions import _positions_lock, flush_positions, has_position_amount, positions

    order_snap = _build_positions_snapshot_from_orders(scope)
    changed = False
    with _positions_lock:
        for key, pos in positions.items():
            if not has_position_amount(pos):
                continue
            osnap = order_snap.get(key)
            if osnap:
                peak = float(osnap.get("peak_amount") or 0)
                sold = float(osnap.get("sold_percent") or 0)
            else:
                peak = float(pos.get("peak_amount") or 0)
                if peak <= 0:
                    peak = float(pos["amount"])
                sold = float(pos.get("sold_percent") or 0)
            if peak <= 0:
                continue
            if (
                float(pos.get("peak_amount") or 0) != peak
                or abs(float(pos.get("sold_percent") or 0) - sold) > 0.001
            ):
                pos["peak_amount"] = peak
                pos["sold_percent"] = sold
                changed = True
    if changed:
        flush_positions(scope=scope, force=True)
        log(f"Reconciled peak_amount for scope={scope}", "INFO")
    return changed


def backfill_orders_from_trade_history(scope: str) -> int:
    """One-off: create filled orders for trades missing from the order ledger."""
    from data_manager import load_orders, load_trade_history_document, save_orders

    data = load_orders(scope)
    orders = list(data.get("orders", []))
    known_ids = {o.get("id") for o in orders if o.get("id")}
    trades = load_trade_history_document(scope).get("trades", [])
    seq = max([int(o.get("display_seq", 0)) for o in orders], default=0)
    added = 0

    for trade in trades:
        order_id = trade.get("order_id") or ""
        if not order_id or order_id in known_ids:
            continue
        ts = trade.get("timestamp", "")
        side = (trade.get("type") or "buy").lower()
        seq += 1
        orders.append(
            {
                "id": order_id,
                "display_seq": seq,
                "status": "filled",
                "side": side,
                "symbol": trade.get("symbol", ""),
                "timeframe": trade.get("timeframe", "4h"),
                "order_type": "market",
                "source": trade.get("source", "auto"),
                "signal": trade.get("signal", ""),
                "trading_mode": trade.get("mode", scope if scope != "demo" else "paper"),
                "ledger_scope": scope,
                "legacy_trade_ts": ts,
                "request": {
                    "price": float(trade.get("price", 0)),
                    "amount": float(trade.get("amount", 0)),
                    "usdt": float(trade.get("usdt_amount", 0) or 0) or None,
                },
                "risk": {
                    "approved": True,
                    "message": "Backfilled from trade history",
                    "code": "",
                    "size_multiplier": 1.0,
                },
                "execution": {
                    "price": float(trade.get("price", 0)),
                    "amount": float(trade.get("amount", 0)),
                    "usdt": float(
                        trade.get("usdt_amount") or trade.get("usdt_received") or 0
                    ),
                    "exchange_order_id": trade.get("exchange_order_id"),
                },
                "pnl": trade.get("pnl"),
                "error": None,
                "timestamps": {"created": ts or "", "updated": ts or "", "filled": ts or ""},
            }
        )
        known_ids.add(order_id)
        added += 1

    if added:
        data["orders"] = orders
        data["ledger_scope"] = scope
        save_orders(data, scope)
        log(f"Backfilled {added} order(s) from trade history for scope={scope}", "INFO")
    return added


def _preserve_legacy_cache_lots(scope: str) -> None:
    """Paper/live only: keep material cache lots not yet represented in orders SOT."""
    if scope == "demo":
        return
    from data_manager import load_positions_document
    from strategies.positions import (
        DUST_AMOUNT_EPSILON,
        _deserialize_position,
        _positions_lock,
        _recompute_open_count,
        positions,
    )

    cache_positions = load_positions_document(scope).get("positions", {}) or {}
    order_snap = _build_positions_snapshot_from_orders(scope)
    with _positions_lock:
        for key, cached in cache_positions.items():
            if key in order_snap or key in positions:
                continue
            if float(cached.get("amount", 0) or 0) <= DUST_AMOUNT_EPSILON:
                continue
            positions[key] = _deserialize_position(dict(cached))
        _recompute_open_count()


def sync_positions_on_startup() -> None:
    """Reconcile peak_amount cache after bootstrap (no wipe/rebuild)."""
    from data_manager import get_config, resolve_ledger_scope

    scope = resolve_ledger_scope(get_config().get("trading_mode", "paper"))
    migrate_legacy_positions()
    _preserve_legacy_cache_lots(scope)
    reconcile_peak_amounts(scope)