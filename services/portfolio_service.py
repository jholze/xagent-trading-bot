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
        return self.execute_sell(
            order.symbol, timeframe, order.price, order.signal or "SELL", order.amount or None,
            source=source, order_id=oid,
        )

    def get_balance_summary(self) -> dict:
        return load_trade_history()