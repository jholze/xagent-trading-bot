"""Unified cash + position replay for simulated live (live-parity fill filter)."""

from __future__ import annotations

_SIM_CASH_EPS = 0.01


def _filled_order_usdt(order: dict) -> float:
    execution = order.get("execution") or {}
    request = order.get("request") or {}
    for section in (execution, request):
        raw = section.get("usdt")
        if raw is not None:
            try:
                val = float(raw)
                if val > 0:
                    return val
            except (TypeError, ValueError):
                pass
    price = float(execution.get("price") or request.get("price") or 0)
    amount = float(execution.get("amount") or request.get("amount") or 0)
    if price > 0 and amount > 0:
        return price * amount
    return 0.0


def _is_dca_order(order: dict) -> bool:
    signal = (order.get("signal") or "").upper()
    source = (order.get("source") or "").lower()
    return signal == "BUY_DCA" or source in (
        "dca",
        "dca_recovery",
        "dca_scheduled",
        "dca_sniper",
    )


def _order_window(order: dict) -> tuple[str | None, str | None]:
    ts = order.get("timestamps") or {}
    created = ts.get("created") or None
    filled = ts.get("filled") or created
    if created == "":
        created = None
    if filled == "":
        filled = created
    return created, filled


def _is_dust_remainder(pos: dict) -> bool:
    from strategies.positions import has_position_amount, is_open_position

    return has_position_amount(pos) and not is_open_position(pos)


def _entry_source_tag(source: str | None) -> str | None:
    """Mirror PortfolioService tagging for entry_sensor + gainer_* sources."""
    s = (source or "").strip()
    if not s:
        return None
    if s == "entry_sensor_15m":
        return s
    try:
        from services.gainer_signal.pure import is_gainer_source

        if is_gainer_source(s):
            return s
    except Exception:
        if s.startswith("gainer_") or s == "gate_prev_top":
            return s
    return None


def _empty_order_position() -> dict:
    return {
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
        "dca_rounds": 0,
        "dca_max_rounds": 0,
        "last_dca_at": None,
        "dca_total_usdt": 0.0,
        "dca_recovery_rounds": 0,
        "dca_recovery_max_rounds": 0,
        "last_dca_recovery_at": None,
        "entry_source": None,
        "entry_at": None,
        "exit_ladder_step": 0,
    }


def _reset_position_cycle(
    pos: dict,
    *,
    amount: float,
    price: float,
    trade_ts: str | None,
    cycle_created: str | None = None,
    cycle_filled: str | None = None,
    fill_price: float | None = None,
) -> None:
    """Start a new cycle. ``price`` is the average (dust stays weighted in).

    ``fill_price`` is the fill itself, used for the peak stamp. Opening-order
    created/filled ride on the snapshot for the K3.1 window.
    """
    px = float(fill_price if fill_price is not None else price)
    from strategies.positions import apply_cycle_field_reset

    apply_cycle_field_reset(pos, fill_price=px, fill_time=trade_ts, new_amount=amount)
    pos["amount"] = amount
    pos["average_entry"] = price
    pos["last_buy_price"] = px
    pos["last_action"] = "BUY"
    pos["last_trade_type"] = "BUY"
    pos["last_trade_at"] = trade_ts
    pos["cycle_open_created"] = cycle_created or trade_ts
    pos["cycle_open_filled"] = cycle_filled or cycle_created or trade_ts


def _replay_peak_reset(pos: dict, fill_price: float, trade_ts: str | None) -> None:
    """F1 replay stamp: fill price and fill time, never the rebuild clock."""
    try:
        from strategies.dca import reanchor_recent_high_after_dca

        reanchor_recent_high_after_dca(pos, float(fill_price))
    except Exception:
        pass
    if trade_ts:
        try:
            from strategies.recovery_hold import stamp_peak_epoch_on_dca

            stamp_peak_epoch_on_dca(pos, float(fill_price), at=trade_ts)
        except Exception:
            pass
        pos["peak_at"] = trade_ts
        pos["peak_epoch_at"] = pos.get("peak_epoch_at") or trade_ts
    pos["v3"] = False


def _apply_acknowledged_buy(
    snapshot: dict,
    order: dict,
    *,
    price: float,
    amount: float,
    trade_ts: str | None,
    source: str,
) -> None:
    from strategies.positions import get_key

    symbol = order.get("symbol", "")
    timeframe = order.get("timeframe", "4h")
    key = get_key(symbol, timeframe)
    pos = snapshot.setdefault(key, _empty_order_position())

    old_amount = float(pos.get("amount") or 0)
    dust = old_amount > 0 and _is_dust_remainder(pos)
    created, filled = _order_window(order)
    if old_amount <= 0 or dust:
        new_amount = old_amount + amount
        if dust:
            old_avg = float(pos.get("average_entry") or price)
            avg = (old_avg * old_amount + price * amount) / new_amount
        else:
            avg = price
        _reset_position_cycle(
            pos,
            amount=new_amount,
            price=avg,
            trade_ts=trade_ts,
            cycle_created=created,
            cycle_filled=filled,
            fill_price=price,
        )
        tagged = _entry_source_tag(source)
        if tagged:
            pos["entry_source"] = tagged
            pos["entry_at"] = trade_ts
    elif _is_dca_order(order):
        old_avg = float(pos.get("average_entry") or price)
        new_avg = (old_avg * old_amount + price * amount) / (old_amount + amount)
        pos["average_entry"] = new_avg
        pos["amount"] = old_amount + amount
        pos["last_buy_price"] = price
        pos["last_action"] = "BUY_DCA"
        pos["last_trade_type"] = "BUY_DCA"
        pos["last_trade_at"] = trade_ts
        pos["dca_rounds"] = int(pos.get("dca_rounds", 0) or 0) + 1
        pos["last_dca_at"] = trade_ts
        pos["dca_total_usdt"] = float(pos.get("dca_total_usdt", 0) or 0) + price * amount
        # Same as live: a recovery fill increments dca_rounds only.
        if abs(new_avg - old_avg) > 1e-12:
            _replay_peak_reset(pos, price, trade_ts)
    else:
        old_avg = float(pos.get("average_entry") or price)
        new_amount = old_amount + amount
        new_avg = (old_avg * old_amount + price * amount) / new_amount
        pos["average_entry"] = new_avg
        pos["amount"] = new_amount
        pos["last_buy_price"] = price
        pos["last_action"] = "BUY"
        pos["last_trade_type"] = "BUY"
        pos["last_trade_at"] = trade_ts
        if not pos.get("entry_source"):
            tagged = _entry_source_tag(source)
            if tagged:
                pos["entry_source"] = tagged
                pos["entry_at"] = pos.get("entry_at") or trade_ts
        if abs(new_avg - old_avg) > 1e-12:
            _replay_peak_reset(pos, price, trade_ts)


def _apply_acknowledged_sell(
    snapshot: dict,
    order: dict,
    *,
    sell_amount: float,
    trade_ts: str | None,
    pnl_scale: float,
) -> float:
    """Apply sell to snapshot; return realized PnL attributed to this fill."""
    from strategies.positions import get_key

    symbol = order.get("symbol", "")
    timeframe = order.get("timeframe", "4h")
    key = get_key(symbol, timeframe)
    pos = snapshot.setdefault(key, _empty_order_position())

    original = pos["amount"]
    pos["amount"] = max(0.0, original - sell_amount)
    peak = float(pos.get("peak_amount") or original or 0)
    if peak > 0:
        pos["sold_percent"] = min(1.0, max(0.0, 1.0 - pos["amount"] / peak))
    pos["last_action"] = "SELL"
    pos["last_trade_type"] = "SELL"
    pos["last_trade_at"] = trade_ts
    signal = (order.get("signal") or "").upper()
    if "PARTIAL" in signal or signal in ("SELL_30", "SELL_20", "SELL_10"):
        pos["_partial_sell_count"] = int(pos.get("_partial_sell_count") or 0) + 1
    pnl = order.get("pnl")
    realized = 0.0
    if pnl is not None:
        realized = float(pnl) * pnl_scale
        pos["realized_pnl"] = float(pos.get("realized_pnl", 0)) + realized
    # Full flat only. A partial sell keeps dca_rounds.
    if float(pos.get("amount") or 0) <= 1e-12:
        pos["dca_rounds"] = 0
        pos["dca_recovery_rounds"] = 0
    return realized


def replay_simulated_ledger(orders: list, initial: float = 5000.0) -> dict:
    """Single chronological replay: cash, positions, realized PnL (live-parity)."""
    balance = float(initial)
    realized_pnl = 0.0
    positions: dict = {}
    sorted_orders = sorted(
        orders or [],
        key=lambda o: (
            (o.get("timestamps") or {}).get("filled")
            or (o.get("timestamps") or {}).get("created")
            or ""
        ),
    )

    for order in sorted_orders:
        if order.get("status") != "filled":
            continue

        side = (order.get("side") or "").lower()
        execution = order.get("execution") or {}
        request = order.get("request") or {}
        price = float(execution.get("price") or request.get("price") or 0)
        # execution.amount is NET qty owned (#333). Old rows without
        # filled_qty_gross still store GROSS here — do not infer a fee.
        amount = float(execution.get("amount") or request.get("amount") or 0)
        trade_ts = (
            order.get("timestamps", {}).get("filled")
            or order.get("timestamps", {}).get("created")
        )
        source = (order.get("source") or "").lower()

        if side == "buy":
            if amount <= 0 or price <= 0:
                continue
            usdt = _filled_order_usdt(order)
            if usdt > balance + _SIM_CASH_EPS:
                continue
            balance -= usdt
            _apply_acknowledged_buy(
                positions,
                order,
                price=price,
                amount=amount,
                trade_ts=trade_ts,
                source=source,
            )
        elif side == "short":
            if amount <= 0 or price <= 0:
                continue
            usdt = _filled_order_usdt(order)
            try:
                lev = float(
                    order.get("leverage")
                    or (request or {}).get("leverage")
                    or (execution or {}).get("leverage")
                    or 2
                )
            except (TypeError, ValueError):
                lev = 2.0
            lev = max(1.0, lev)
            margin = usdt / lev if lev else usdt
            if margin > balance + _SIM_CASH_EPS:
                continue
            balance -= margin
            from strategies.positions import get_key

            symbol = order.get("symbol", "")
            timeframe = order.get("timeframe", "4h")
            if not symbol:
                continue
            key = get_key(symbol, timeframe)
            pos = positions.setdefault(key, _empty_order_position())
            old = float(pos.get("amount") or 0)
            if old <= 0 or str(pos.get("side") or "") != "short":
                created, filled = _order_window(order)
                pos.update({
                    "amount": amount,
                    "peak_amount": amount,
                    "average_entry": price,
                    "side": "short",
                    "leverage": lev,
                    "sold_percent": 0.0,
                    "last_action": "SHORT",
                    "last_trade_type": "SHORT",
                    "last_trade_at": trade_ts,
                    "first_buy_at": trade_ts,
                    "entry_at": trade_ts,
                    "recent_low": price,
                    "v3": False,
                    "cycle_open_created": created or trade_ts,
                    "cycle_open_filled": filled or trade_ts,
                })
            else:
                new_a = old + amount
                pos["average_entry"] = (float(pos.get("average_entry") or price) * old + price * amount) / new_a
                pos["amount"] = new_a
                pos["leverage"] = lev
                pos["side"] = "short"
                pos["last_action"] = "SHORT"
                pos["last_trade_at"] = trade_ts
        elif side == "cover":
            if amount <= 0:
                continue
            from strategies.positions import get_key

            symbol = order.get("symbol", "")
            timeframe = order.get("timeframe", "4h")
            if not symbol:
                continue
            key = get_key(symbol, timeframe)
            pos = positions.get(key)
            if not pos or str(pos.get("side") or "") != "short":
                continue
            original = float(pos.get("amount") or 0)
            if original <= _SIM_CASH_EPS:
                continue
            cover_amt = min(amount, original)
            entry = float(pos.get("average_entry") or price)
            lev = float(pos.get("leverage") or 2) or 2.0
            frac = cover_amt / original if original else 1.0
            margin_back = (original * entry / lev) * frac
            pnl = cover_amt * (entry - price)
            balance += margin_back + pnl
            pos["amount"] = original - cover_amt
            pos["last_action"] = "COVER"
            pos["last_trade_type"] = "COVER"
            pos["last_trade_at"] = trade_ts
            pos["realized_pnl"] = float(pos.get("realized_pnl") or 0) + pnl
            if pos["amount"] <= _SIM_CASH_EPS:
                pos["amount"] = 0.0
                pos["side"] = "long"
                pos["leverage"] = None
            realized_pnl += pnl
        elif side == "sell":
            if amount <= 0:
                continue
            from strategies.positions import get_key

            symbol = order.get("symbol", "")
            timeframe = order.get("timeframe", "4h")
            if not symbol:
                continue
            key = get_key(symbol, timeframe)
            pos = positions.get(key)
            if not pos:
                continue
            if str(pos.get("side") or "").lower() == "short":
                continue
            original = float(pos.get("amount") or 0)
            if original <= _SIM_CASH_EPS:
                continue
            sell_amount = min(amount, original)
            order_usdt = _filled_order_usdt(order)
            pnl_scale = sell_amount / amount if amount > 0 else 1.0
            cash_proceeds = order_usdt * pnl_scale
            balance += cash_proceeds
            realized_pnl += _apply_acknowledged_sell(
                positions,
                order,
                sell_amount=sell_amount,
                trade_ts=trade_ts,
                pnl_scale=pnl_scale,
            )

    open_positions = {
        key: pos
        for key, pos in positions.items()
        if float(pos.get("amount") or 0) > _SIM_CASH_EPS
    }
    return {
        "cash": round(max(0.0, balance), 8),
        "positions": open_positions,
        "realized_pnl": round(realized_pnl, 8),
    }