from datetime import datetime

from core.config import get_bot_config
from core.costs import (
    COST_MODEL_VERSION,
    CostModel,
    Fill,
    fee_rate_field,
    read_rate_fraction,
    swap_taker_estimate_rate,
    trade_cost_fields,
    write_rate_fraction,
)
from core.models import TradeResult, TradeOrder, ctx_float_or_none, trade_ctx_fields
from data_manager import load_trade_history, record_trade
from logger import log
from strategies.positions import (
    bind_buy_timeframe,
    flush_positions,
    get_position,
    refresh_venue_min_mark,
    sell_fraction_for_signal,
    set_position_field,
    update_position,
)

_QTY_EPS = 1e-12


def _default_entry_source(source: str | None) -> str | None:
    """Sources that must be tagged on the open lot for guards/caps.

    entry_sensor_15m: sell-guard tagging.
    gainer_*: concurrent open-slot caps (WS-2 balloon).
    """
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


def _ctx_ledger_fields(ctx: dict | None) -> dict:
    """Flat diagnostic ctx axes for trade-history rows. Always present."""
    bag = ctx if isinstance(ctx, dict) else {}
    return {
        "ctx_oracle_state": bag.get("ctx_oracle_state"),
        "ctx_coin_regime": bag.get("ctx_coin_regime"),
        "ctx_volume_rel": ctx_float_or_none(bag.get("ctx_volume_rel")),
        "ctx_volume_window_days": ctx_float_or_none(bag.get("ctx_volume_window_days")),
    }


def _cover_funding_usdt(
    symbol: str,
    pos: dict,
    qty: float,
    entry: float,
    *,
    config_raw: dict | None,
) -> float:
    """Paper funding deducted from COVER PnL. Raises on unparsable hold time or rate."""
    from datetime import timezone as _tz
    from strategies.short_math import funding_cost_usdt, notional_usdt
    from strategies.short_policy import resolve_short_params

    opened = pos.get("entry_at") or pos.get("first_buy_at")
    hours = 0.0
    if opened:
        t0 = datetime.fromisoformat(str(opened).replace("Z", "+00:00"))
        if t0.tzinfo is None:
            t0 = t0.replace(tzinfo=_tz.utc)
        hours = max(0.0, (datetime.now(_tz.utc) - t0).total_seconds() / 3600.0)
    params = resolve_short_params(symbol=symbol, lot=pos, config_raw=config_raw)
    return funding_cost_usdt(
        notional_usdt(qty, entry),
        hours,
        float(params.get("funding_rate_8h") or 0),
    )


def _booking_rows(config, order_id: str | None, typ: str) -> list[dict]:
    """Trade rows already booked for this order_id and type.

    Live mode writes ``record_live_trade``; paper/demo writes ``record_trade``.
    Read the ledger that this mode actually appends to, so a partial fill's
    later remainder is a delta and a replay of the same cumulative qty is not.
    """
    if not order_id:
        return []
    from data_manager import load_live_trade_history, load_trade_history, uses_exchange_ledger

    mode = getattr(config, "trading_mode", None)
    if uses_exchange_ledger(mode):
        history = load_live_trade_history() or {}
    else:
        history = load_trade_history() or {}
    typ_u = str(typ or "").upper()
    rows: list[dict] = []
    for row in history.get("trades") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("order_id") or "") != str(order_id):
            continue
        if str(row.get("type") or "").upper() != typ_u:
            continue
        rows.append(row)
    return rows


def _row_qty(row: dict) -> float:
    try:
        return float(row.get("amount") or 0)
    except (TypeError, ValueError):
        return 0.0


def _lot_open_fee(config, pos: dict, lot_qty: float, entry: float) -> tuple[float, bool]:
    """Stored opening fee, or a max-rule estimate when a tracked lot lost it.

    A missing ``open_fee_usdt`` on a lot opened after #685 is not zero (C5).
    Legacy lots have no marker and stay at 0. The bool is True when the
    number was estimated (or could not be, which still needs reconcile).
    """
    tracked = bool(pos.get("open_fee_tracked"))
    if not tracked or "open_fee_usdt" in pos:
        try:
            return float(pos.get("open_fee_usdt") or 0), False
        except (TypeError, ValueError):
            return 0.0, False
    picked = swap_taker_estimate_rate(config)
    try:
        notional = float(lot_qty) * float(entry)
    except (TypeError, ValueError):
        notional = 0.0
    if not picked or notional <= 0:
        log("open_fee_unresolved tracked lot has no rate or notional", "ERROR")
        return 0.0, True
    rate = float(picked[0])
    if rate <= 0:
        log("open_fee_unresolved tracked lot has no positive rate", "ERROR")
        return 0.0, True
    return notional * rate, True


def _row_fee(row: dict) -> float:
    for key in ("fee_usdt", "fee"):
        raw = row.get(key)
        if raw is None or raw == "":
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return 0.0


def _replay_result(symbol: str, typ: str, order_id: str, rows: list[dict]) -> TradeResult:
    last = rows[-1] if rows else {}
    if not isinstance(last, dict) or "pnl" not in last or last.get("pnl") is None:
        replay_pnl = None
    else:
        try:
            replay_pnl = float(last.get("pnl"))
        except (TypeError, ValueError):
            replay_pnl = None
    result = TradeResult(
        True,
        typ,
        symbol,
        amount=float(_row_qty(last)),
        price=float(last.get("price") or 0),
        usdt_amount=float(last.get("usdt_amount") or last.get("usdt_received") or 0),
        pnl=replay_pnl,
        order_id=order_id or "",
        fee=float(_row_fee(last)),
        message="already booked",
    )
    result.booking_skipped = True
    result.fee_estimated = bool(last.get("fee_estimated"))
    result.fee_rate_source = str(last.get("fee_rate_source") or "")
    write_rate_fraction(result, read_rate_fraction(last))
    if typ == "COVER":
        result.funding_usdt = last.get("funding_usdt")
        result.funding_unknown = bool(last.get("funding_unknown"))
        result.open_fee_usdt = float(last.get("open_fee_usdt") or 0)
    return result


class PortfolioService:
    """Single entry point for position and trade ledger updates."""

    def __init__(self, config=None):
        self.config = config or get_bot_config()

    def execute_buy(
        self,
        symbol: str,
        timeframe: str,
        price: float,
        usdt_amount: float = None,
        source: str = "auto",
        order_id: str = None,
        sync_virtual_ledger: bool = True,
        entry_source: str | None = None,
        entry_15m_vol_ratio: float | None = None,
        fill: Fill | None = None,
        ctx: dict | None = None,
        fee_unknown: bool = False,
        order_type: str | None = None,
    ) -> TradeResult:
        if price <= 0:
            return TradeResult(False, "BUY", symbol, message="Invalid price")
        from strategies.short_math import is_short

        timeframe = bind_buy_timeframe(symbol, timeframe)
        existing = get_position(symbol, timeframe)
        if is_short(existing) and float(existing.get("amount") or 0) > 1e-12:
            return TradeResult(
                False, "BUY", symbol, message="one-way: cover short before buy",
            )
        usdt = usdt_amount or self.config.max_usdt_per_trade
        if fee_unknown:
            # F7: config fees only. The copy is at this call site so auto
            # fee_source cannot fetch exchange fees on the basis.
            raw_in = self.config.raw if hasattr(self.config, "raw") else self.config
            raw = dict(raw_in) if isinstance(raw_in, dict) else {}
            costs = raw.get("costs")
            costs = dict(costs) if isinstance(costs, dict) else {}
            costs["fee_source"] = "config"
            raw["costs"] = costs
            cm = CostModel.from_config(raw, symbol=symbol)
            f = fill if fill is not None else cm.simulate_buy(price, usdt=usdt)
        else:
            cm = CostModel.from_config(self.config, symbol=symbol)
            f = fill if fill is not None else cm.simulate_buy(price, usdt=usdt)
        if f.qty_net <= 0:
            return TradeResult(False, "BUY", symbol, message="Fill qty_net is 0")
        amount = f.qty_net
        prior_marker = bool(existing.get("basis_fee_estimated"))
        if fee_unknown:
            gross_px = float(f.fill_price or price)
            net_entry = cm.estimated_buy_entry(gross_px, order_type or "market")
        else:
            net_entry = f.quote_net / f.qty_net
        signal = "BUY_DCA" if source in ("dca", "dca_recovery") else "BUY"
        effective_entry_source = entry_source or _default_entry_source(source)
        update_position(
            symbol,
            timeframe,
            signal,
            net_entry,
            amount,
            entry_source=effective_entry_source,
            entry_15m_vol_ratio=entry_15m_vol_ratio,
            fee_estimated=bool(fee_unknown),
        )
        try:
            # The fill is a fresh mark. A merged lot at or above the lift
            # threshold leaves the below-min state; the brake flag stays.
            refresh_venue_min_mark(
                symbol, timeframe, mark=float(price), mark_fresh=True
            )
        except Exception as exc:
            log(f"venue min refresh after buy failed {symbol}: {exc}", "WARNING")
        if fee_unknown and not prior_marker:
            from core.tenant_context import resolve_tenant_id

            log(
                f"basis fee estimated tenant={resolve_tenant_id()} symbol={symbol} "
                f"trigger=fee_unknown",
                "WARNING",
            )
        if sync_virtual_ledger:
            record_trade({
                "type": "BUY",
                "symbol": symbol,
                "price": f.fill_price,
                "amount": amount,
                "usdt_amount": f.quote_net,
                "source": source,
                "order_id": order_id,
                "timestamp": datetime.now().isoformat(),
                **trade_cost_fields(f),
                **_ctx_ledger_fields(ctx),
            })
        return TradeResult(
            True, "BUY", symbol, amount=amount, price=f.fill_price,
            usdt_amount=f.quote_net, order_id=order_id or "", fee=f.fee_usdt,
        )

    def execute_sell(
        self,
        symbol: str,
        timeframe: str,
        price: float,
        signal: str,
        amount: float = None,
        source: str = "auto",
        order_id: str = None,
        sync_virtual_ledger: bool = True,
        fill: Fill | None = None,
        ctx: dict | None = None,
        exit_source: str | None = None,
        fee_unknown: bool = False,
    ) -> TradeResult:
        if price <= 0:
            return TradeResult(False, "SELL", symbol, message="Invalid price")
        pos = get_position(symbol, timeframe)
        from strategies.short_math import is_short

        if is_short(pos) and float(pos.get("amount") or 0) > 1e-12:
            return TradeResult(
                False, "SELL", symbol, message="one-way: cover short — do not sell",
            )
        if amount is None:
            fraction = sell_fraction_for_signal(signal)
            amount = float(pos["amount"]) * fraction
        if amount <= 0:
            return TradeResult(False, "SELL", symbol, message="No position to sell")
        cm = CostModel.from_config(self.config, symbol=symbol)
        if fee_unknown and fill is None:
            # F8: no sell-fee estimate. Credit the gross quote.
            gross_qty = float(amount)
            gross_quote = float(price) * gross_qty
            f = Fill(
                side="sell",
                order_type="market",
                request_price=float(price),
                fill_price=float(price),
                qty_gross=gross_qty,
                qty_net=gross_qty,
                quote_gross=gross_quote,
                quote_net=gross_quote,
                fee_base=0.0,
                fee_quote=0.0,
                fee_usdt=0.0,
                slippage_usdt=0.0,
            )
        else:
            f = fill if fill is not None else cm.simulate_sell(price, amount)
        qty_sold = f.qty_net if f.qty_net > 0 else amount
        received = f.quote_net
        entry = pos.get("average_entry", price)
        pnl = CostModel.realized_pnl(qty_sold=qty_sold, avg_entry_net=entry, sell=f)
        if fee_unknown:
            pnl_basis = "sell_fee_unknown"
        elif pos.get("basis_fee_estimated"):
            pnl_basis = "fee_estimated"
        else:
            pnl_basis = "net"
        pnl_fee_source = str(getattr(cm.params, "source", "") or "config")
        update_position(
            symbol, timeframe, signal, f.fill_price, qty_sold, exit_source=exit_source
        )
        if source == "cmc":
            from strategies.positions import save_positions, set_position_field

            set_position_field(symbol, timeframe, "last_cmc_sell_at", datetime.now().isoformat())
            save_positions()
        if sync_virtual_ledger:
            record_trade({
                "type": "SELL",
                "symbol": symbol,
                "price": f.fill_price,
                "amount": qty_sold,
                "usdt_received": received,
                "pnl": pnl,
                "pnl_basis": pnl_basis,
                "pnl_fee_source": pnl_fee_source,
                "source": source,
                "order_id": order_id,
                "timestamp": datetime.now().isoformat(),
                **trade_cost_fields(f),
                **_ctx_ledger_fields(ctx),
            })
        return TradeResult(
            True, "SELL", symbol, amount=qty_sold, price=f.fill_price,
            usdt_amount=received, pnl=pnl, order_id=order_id or "", fee=f.fee_usdt,
            pnl_basis=pnl_basis, pnl_fee_source=pnl_fee_source,
        )

    def execute_short(
        self,
        symbol: str,
        timeframe: str,
        price: float,
        usdt_amount: float = None,
        source: str = "manual",
        order_id: str = None,
        leverage: float | None = None,
        entry_source: str | None = None,
        sync_virtual_ledger: bool = True,
        ctx: dict | None = None,
        exit_source: str | None = None,
        short_recipe: str | None = None,
        fill: Fill | None = None,
        fee_usdt: float | None = None,
        fee_estimated: bool = False,
        fee_rate_source: str = "",
        rate: float = 0.0,
    ) -> TradeResult:
        if price <= 0:
            return TradeResult(False, "SHORT", symbol, message="Invalid price")
        from strategies.short_math import clamp_leverage, is_short, margin_usdt
        from strategies.short_policy import resolve_short_params

        existing = get_position(symbol, timeframe)
        from strategies.positions import is_open_position

        if is_open_position(existing) and not is_short(existing):
            return TradeResult(
                False, "SHORT", symbol, message="one-way: close long before short",
            )

        params = resolve_short_params(symbol=symbol, config_raw=self.config.raw)
        lev = clamp_leverage(leverage or params["leverage"], cap=params["leverage_cap"])
        notional = usdt_amount or self.config.max_usdt_per_trade
        reported_qty = notional / price if price else 0.0
        reported_fee = fee_usdt
        if reported_fee is None and fill is not None:
            reported_fee = float(fill.fee_usdt or 0)
        if reported_fee is None:
            reported_fee = 0.0
        if fill is not None and getattr(fill, "fee_estimated", False):
            fee_estimated = True
            fee_rate_source = fee_rate_source or str(getattr(fill, "fee_rate_source", "") or "")
            if not rate:
                rate = read_rate_fraction(fill)

        rows = _booking_rows(self.config, order_id, "SHORT") if order_id else []
        booked_qty = sum(_row_qty(row) for row in rows)
        # Same order_id books only the qty above what is already stored (K1).
        if order_id:
            delta = reported_qty - booked_qty
            if delta <= _QTY_EPS:
                return _replay_result(symbol, "SHORT", order_id, rows)
            amount = delta
            booked_fee = sum(_row_fee(row) for row in rows)
            fee_slice = reported_fee - booked_fee if booked_qty > _QTY_EPS else reported_fee
            if fee_slice < 0:
                fee_slice = 0.0
            notional = price * amount
        else:
            amount = reported_qty
            fee_slice = reported_fee
        margin = margin_usdt(amount, price, lev) if price else 0.0
        adding = is_short(existing) and float(existing.get("amount") or 0) > _QTY_EPS
        prior_fee = float(existing.get("open_fee_usdt") or 0) if adding else 0.0
        prior_est = bool(existing.get("open_fee_estimated")) if adding else False
        open_fee = prior_fee + float(fee_slice or 0)
        open_est = bool(prior_est or fee_estimated)
        update_position(
            symbol,
            timeframe,
            "SHORT",
            price,
            amount,
            entry_source=entry_source or source,
            leverage=lev,
            short_recipe=short_recipe,
            exit_source=exit_source,
            open_fee_tracked=True,
        )
        set_position_field(symbol, timeframe, "open_fee_usdt", open_fee)
        set_position_field(symbol, timeframe, "open_fee_estimated", open_est)
        # update_position already flushed the lot without these fields.
        flush_positions(force=True)
        fee_fields = {
            "fee": float(fee_slice or 0),
            "fee_usdt": float(fee_slice or 0),
            "open_fee_usdt": open_fee,
            "fee_estimated": open_est,
            "fee_rate_source": str(fee_rate_source or ""),
            **fee_rate_field(rate),
        }
        if sync_virtual_ledger:
            record_trade({
                "type": "SHORT",
                "symbol": symbol,
                "price": price,
                "amount": amount,
                "usdt_amount": notional,
                "margin_usdt": margin,
                "leverage": lev,
                "source": source,
                "order_id": order_id,
                "timestamp": datetime.now().isoformat(),
                "cost_model": COST_MODEL_VERSION,
                **fee_fields,
                **_ctx_ledger_fields(ctx),
            })
        result = TradeResult(
            True, "SHORT", symbol, amount=amount, price=price, usdt_amount=notional,
            order_id=order_id or "", fee=float(fee_slice or 0),
            # A short open has no realized pnl. The dataclass default 0.0 would
            # store a fake zero on the order row.
            pnl=None,
        )
        result.fee_estimated = bool(fee_estimated)
        result.fee_rate_source = str(fee_rate_source or "")
        write_rate_fraction(result, rate)
        result.open_fee_usdt = open_fee
        return result

    def execute_cover(
        self,
        symbol: str,
        timeframe: str,
        price: float,
        amount: float = None,
        source: str = "manual",
        order_id: str = None,
        sync_virtual_ledger: bool = True,
        ctx: dict | None = None,
        fill: Fill | None = None,
        fee_usdt: float | None = None,
        fee_estimated: bool = False,
        fee_rate_source: str = "",
        rate: float = 0.0,
    ) -> TradeResult:
        if price <= 0:
            return TradeResult(False, "COVER", symbol, message="Invalid price")
        from strategies.short_math import is_short, margin_usdt, unrealized_pnl

        reported_fee = fee_usdt
        if reported_fee is None and fill is not None:
            reported_fee = float(fill.fee_usdt or 0)
        if reported_fee is None:
            reported_fee = 0.0
        if fill is not None and getattr(fill, "fee_estimated", False):
            fee_estimated = True
            fee_rate_source = fee_rate_source or str(getattr(fill, "fee_rate_source", "") or "")
            if not rate:
                rate = read_rate_fraction(fill)

        rows = _booking_rows(self.config, order_id, "COVER") if order_id else []
        booked_qty = sum(_row_qty(row) for row in rows)
        reported_qty = float(amount) if amount and amount > 0 else None
        # Replay of the same cumulative cover qty must not require the lot
        # to still be open and must not write another row.
        if order_id and reported_qty is not None:
            delta = reported_qty - booked_qty
            if delta <= _QTY_EPS and booked_qty > _QTY_EPS:
                return _replay_result(symbol, "COVER", order_id, rows)
            cover_qty = delta
            booked_fee = sum(_row_fee(row) for row in rows)
            close_fee = reported_fee - booked_fee if booked_qty > _QTY_EPS else reported_fee
            if close_fee < 0:
                close_fee = 0.0
        else:
            cover_qty = reported_qty
            close_fee = reported_fee

        pos = get_position(symbol, timeframe)
        if not is_short(pos) or float(pos.get("amount") or 0) <= 0:
            return TradeResult(False, "COVER", symbol, message="No short to cover")
        lot_qty = float(pos["amount"])
        qty = float(cover_qty) if cover_qty and cover_qty > 0 else lot_qty
        qty = min(qty, lot_qty)
        entry = float(pos.get("average_entry") or price)
        # Missing key on a post-#685 lot is estimated. Do not read it as 0.
        lot_open_fee, open_fee_missing = _lot_open_fee(self.config, pos, lot_qty, entry)
        open_share = lot_open_fee * (qty / lot_qty) if lot_qty > _QTY_EPS else 0.0
        if open_share < 0:
            open_share = 0.0
        if open_share > lot_open_fee:
            open_share = lot_open_fee
        pnl = unrealized_pnl("short", qty, entry, price)
        funding_usdt: float | None = None
        funding_unknown = True
        try:
            fund = _cover_funding_usdt(
                symbol, pos, qty, entry, config_raw=self.config.raw,
            )
            pnl -= fund
            funding_usdt = fund
            funding_unknown = False
        except Exception as exc:
            log(
                f"COVER funding treated as 0 for {symbol} {timeframe}: {exc!r}",
                "WARNING",
            )
        pnl -= open_share
        pnl -= float(close_fee or 0)
        update_position(symbol, timeframe, "COVER", price, qty)
        remaining_fee = lot_open_fee - open_share
        if remaining_fee < _QTY_EPS:
            remaining_fee = 0.0
        still_open = float(get_position(symbol, timeframe).get("amount") or 0) > _QTY_EPS
        set_position_field(
            symbol, timeframe, "open_fee_usdt", remaining_fee if still_open else 0.0,
        )
        if not still_open:
            set_position_field(symbol, timeframe, "open_fee_estimated", False)
        flush_positions(force=True)
        open_estimated = (
            bool(pos.get("open_fee_estimated")) or open_fee_missing
        ) and open_share > 0
        fee_fields = {
            "fee": float(close_fee or 0),
            "fee_usdt": float(close_fee or 0),
            "open_fee_usdt": open_share,
            "open_fee_estimated": open_estimated,
            "fee_estimated": bool(fee_estimated),
            "fee_rate_source": str(fee_rate_source or ""),
            **fee_rate_field(rate),
        }
        if sync_virtual_ledger:
            record_trade({
                "type": "COVER",
                "symbol": symbol,
                "price": price,
                "amount": qty,
                "usdt_amount": price * qty,
                "margin_usdt": margin_usdt(qty, entry, float(pos.get("leverage") or 2) or 2.0),
                "pnl": pnl,
                "funding_usdt": funding_usdt,
                "funding_unknown": funding_unknown,
                "source": source,
                "order_id": order_id,
                "timestamp": datetime.now().isoformat(),
                "cost_model": COST_MODEL_VERSION,
                **fee_fields,
                **_ctx_ledger_fields(ctx),
            })
        result = TradeResult(
            True, "COVER", symbol, amount=qty, price=price, usdt_amount=price * qty, pnl=pnl,
            order_id=order_id or "", fee=float(close_fee or 0),
            funding_usdt=funding_usdt, funding_unknown=funding_unknown,
        )
        result.fee_estimated = bool(fee_estimated)
        result.fee_rate_source = str(fee_rate_source or "")
        write_rate_fraction(result, rate)
        result.open_fee_usdt = open_share
        result.open_fee_estimated = open_estimated
        if open_fee_missing:
            result.needs_reconcile = True
        return result

    def execute_order(self, order: TradeOrder, timeframe: str = "4h") -> TradeResult:
        source = order.source or "auto"
        oid = order.order_id or None
        ctx = trade_ctx_fields(order)
        if order.type == "BUY":
            return self.execute_buy(
                order.symbol,
                timeframe,
                order.price,
                order.usdt_amount or None,
                source=source,
                order_id=oid,
                entry_15m_vol_ratio=order.entry_15m_vol_ratio,
                ctx=ctx,
            )
        if order.type == "SHORT":
            return self.execute_short(
                order.symbol,
                timeframe,
                order.price,
                order.usdt_amount or None,
                source=source,
                order_id=oid,
                leverage=getattr(order, "leverage", None),
                entry_source=order.signal or source,
                ctx=ctx,
                exit_source=getattr(order, "exit_source", "") or None,
            )
        if order.type == "COVER":
            return self.execute_cover(
                order.symbol,
                timeframe,
                order.price,
                order.amount or None,
                source=source,
                order_id=oid,
                ctx=ctx,
            )
        if order.type != "SELL":
            return TradeResult(False, order.type, order.symbol, message=f"Unknown order type {order.type}")
        return self.execute_sell(
            order.symbol, timeframe, order.price, order.signal or "SELL", order.amount or None,
            source=source, order_id=oid, ctx=ctx,
            exit_source=getattr(order, "exit_source", None) or None,
        )

    def get_balance_summary(self) -> dict:
        return load_trade_history()