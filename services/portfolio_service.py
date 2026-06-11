from datetime import datetime

from core.config import get_bot_config
from core.models import TradeResult, TradeOrder
from data_manager import load_trade_history, record_trade
from strategies.positions import get_position, sell_fraction_for_signal, update_position


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
    ) -> TradeResult:
        if price <= 0:
            return TradeResult(False, "BUY", symbol, message="Invalid price")
        usdt = usdt_amount or self.config.max_usdt_per_trade
        amount = usdt / price
        update_position(symbol, timeframe, "BUY", price, amount)
        record_trade({
            "type": "BUY",
            "symbol": symbol,
            "price": price,
            "amount": amount,
            "usdt_amount": usdt,
            "source": source,
            "timestamp": datetime.now().isoformat(),
        })
        return TradeResult(True, "BUY", symbol, amount=amount, price=price, usdt_amount=usdt)

    def execute_sell(
        self,
        symbol: str,
        timeframe: str,
        price: float,
        signal: str,
        amount: float = None,
        source: str = "auto",
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
        record_trade({
            "type": "SELL",
            "symbol": symbol,
            "price": price,
            "amount": amount,
            "usdt_received": received,
            "pnl": pnl,
            "source": source,
            "timestamp": datetime.now().isoformat(),
        })
        return TradeResult(True, "SELL", symbol, amount=amount, price=price, usdt_amount=received, pnl=pnl)

    def execute_order(self, order: TradeOrder, timeframe: str = "4h") -> TradeResult:
        source = order.source or "auto"
        if order.type == "BUY":
            return self.execute_buy(order.symbol, timeframe, order.price, order.usdt_amount or None, source=source)
        return self.execute_sell(
            order.symbol, timeframe, order.price, order.signal or "SELL", order.amount or None, source=source,
        )

    def get_balance_summary(self) -> dict:
        return load_trade_history()