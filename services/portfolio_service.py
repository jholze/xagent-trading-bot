from datetime import datetime

from core.config import get_bot_config
from core.models import TradeResult, TradeOrder
from data_manager import load_trade_history, record_trade
from strategies.positions import get_position, sell_fraction_for_signal, update_position


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
    ) -> TradeResult:
        if price <= 0:
            return TradeResult(False, "BUY", symbol, message="Invalid price")
        usdt = usdt_amount or self.config.max_usdt_per_trade
        amount = usdt / price
        signal = "BUY_DCA" if source in ("dca", "dca_recovery") else "BUY"
        effective_entry_source = entry_source or _default_entry_source(source)
        update_position(
            symbol,
            timeframe,
            signal,
            price,
            amount,
            entry_source=effective_entry_source,
            entry_15m_vol_ratio=entry_15m_vol_ratio,
        )
        if sync_virtual_ledger:
            record_trade({
                "type": "BUY",
                "symbol": symbol,
                "price": price,
                "amount": amount,
                "usdt_amount": usdt,
                "source": source,
                "order_id": order_id,
                "timestamp": datetime.now().isoformat(),
            })
        return TradeResult(True, "BUY", symbol, amount=amount, price=price, usdt_amount=usdt, order_id=order_id or "")

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
    ) -> TradeResult:
        if price <= 0:
            return TradeResult(False, "SELL", symbol, message="Invalid price")
        pos = get_position(symbol, timeframe)
        if amount is None:
            fraction = sell_fraction_for_signal(signal)
            amount = float(pos["amount"]) * fraction
        if amount <= 0:
            return TradeResult(False, "SELL", symbol, message="No position to sell")
        received = price * amount * (1 - self.config.slippage_percent / 100)
        entry = pos.get("average_entry", price)
        pnl = (price - entry) * amount
        update_position(symbol, timeframe, signal, price, amount)
        if source == "cmc":
            from strategies.positions import save_positions, set_position_field

            set_position_field(symbol, timeframe, "last_cmc_sell_at", datetime.now().isoformat())
            save_positions()
        if sync_virtual_ledger:
            record_trade({
                "type": "SELL",
                "symbol": symbol,
                "price": price,
                "amount": amount,
                "usdt_received": received,
                "pnl": pnl,
                "source": source,
                "order_id": order_id,
                "timestamp": datetime.now().isoformat(),
            })
        return TradeResult(
            True, "SELL", symbol, amount=amount, price=price, usdt_amount=received, pnl=pnl, order_id=order_id or "",
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
    ) -> TradeResult:
        if price <= 0:
            return TradeResult(False, "SHORT", symbol, message="Invalid price")
        from strategies.short_math import clamp_leverage, margin_usdt
        from strategies.short_policy import resolve_short_params

        params = resolve_short_params(symbol=symbol, config_raw=self.config.raw)
        lev = clamp_leverage(leverage or params["leverage"], cap=params["leverage_cap"])
        notional = usdt_amount or self.config.max_usdt_per_trade
        margin = margin_usdt(notional / price, price, lev) if price else 0.0
        amount = notional / price
        update_position(
            symbol,
            timeframe,
            "SHORT",
            price,
            amount,
            entry_source=entry_source or source,
            leverage=lev,
        )
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
            })
        return TradeResult(
            True, "SHORT", symbol, amount=amount, price=price, usdt_amount=notional, order_id=order_id or "",
        )

    def execute_cover(
        self,
        symbol: str,
        timeframe: str,
        price: float,
        amount: float = None,
        source: str = "manual",
        order_id: str = None,
        sync_virtual_ledger: bool = True,
    ) -> TradeResult:
        if price <= 0:
            return TradeResult(False, "COVER", symbol, message="Invalid price")
        pos = get_position(symbol, timeframe)
        from strategies.short_math import is_short, unrealized_pnl

        if not is_short(pos) or float(pos.get("amount") or 0) <= 0:
            return TradeResult(False, "COVER", symbol, message="No short to cover")
        qty = float(amount) if amount and amount > 0 else float(pos["amount"])
        qty = min(qty, float(pos["amount"]))
        entry = float(pos.get("average_entry") or price)
        pnl = unrealized_pnl("short", qty, entry, price)
        try:
            from datetime import timezone as _tz
            from strategies.short_math import funding_cost_usdt, notional_usdt
            from strategies.short_policy import resolve_short_params

            opened = pos.get("entry_at") or pos.get("first_buy_at")
            hours = 0.0
            if opened:
                try:
                    t0 = datetime.fromisoformat(str(opened).replace("Z", "+00:00"))
                    if t0.tzinfo is None:
                        t0 = t0.replace(tzinfo=_tz.utc)
                    hours = max(0.0, (datetime.now(_tz.utc) - t0).total_seconds() / 3600.0)
                except Exception:
                    hours = 0.0
            params = resolve_short_params(symbol=symbol, lot=pos, config_raw=self.config.raw)
            fund = funding_cost_usdt(
                notional_usdt(qty, entry),
                hours,
                float(params.get("funding_rate_8h") or 0),
            )
            pnl -= fund
        except Exception:
            pass
        update_position(symbol, timeframe, "COVER", price, qty)
        if sync_virtual_ledger:
            record_trade({
                "type": "COVER",
                "symbol": symbol,
                "price": price,
                "amount": qty,
                "usdt_amount": price * qty,
                "pnl": pnl,
                "source": source,
                "order_id": order_id,
                "timestamp": datetime.now().isoformat(),
            })
        return TradeResult(
            True, "COVER", symbol, amount=qty, price=price, usdt_amount=price * qty, pnl=pnl, order_id=order_id or "",
        )

    def execute_order(self, order: TradeOrder, timeframe: str = "4h") -> TradeResult:
        source = order.source or "auto"
        oid = order.order_id or None
        if order.type == "BUY":
            return self.execute_buy(
                order.symbol,
                timeframe,
                order.price,
                order.usdt_amount or None,
                source=source,
                order_id=oid,
                entry_15m_vol_ratio=order.entry_15m_vol_ratio,
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
            )
        if order.type == "COVER":
            return self.execute_cover(
                order.symbol,
                timeframe,
                order.price,
                order.amount or None,
                source=source,
                order_id=oid,
            )
        if order.type != "SELL":
            return TradeResult(False, order.type, order.symbol, message=f"Unknown order type {order.type}")
        return self.execute_sell(
            order.symbol, timeframe, order.price, order.signal or "SELL", order.amount or None,
            source=source, order_id=oid,
        )

    def get_balance_summary(self) -> dict:
        return load_trade_history()