from datetime import datetime

from core.config import get_bot_config
from core.models import MarketContext, TradeOrder
from data_manager import get_text, load_trade_history
from execution.paper_adapter import PaperExecutionAdapter
from services.market_service import MarketService
from services.portfolio_service import PortfolioService
from strategies.positions import count_open_positions, get_position
from strategies.registry import get_strategy
class SignalOrchestrator:
    """Coordinates analysis, execution, and notification without strategy↔telegram coupling."""

    def __init__(
        self,
        market_service: MarketService = None,
        portfolio: PortfolioService = None,
        execution_adapter=None,
        notify_callback=None,
    ):
        self.config = get_bot_config()
        self.market = market_service or MarketService()
        self.portfolio = portfolio or PortfolioService(self.config)
        self.execution = execution_adapter or PaperExecutionAdapter(self.portfolio)
        self.notify_callback = notify_callback

    def build_market_context(self, coin: dict, current_price: float) -> MarketContext:
        symbol = coin["symbol"]
        tf = coin.get("timeframe", "4h")
        indicators = self.market.fetch_indicators(symbol, tf, current_price)
        pos = get_position(symbol, tf)
        return MarketContext(
            symbol=symbol,
            timeframe=tf,
            current_price=current_price,
            rsi=indicators["rsi"],
            lower_bb=indicators["lower_bb"],
            vol_multiplier=indicators["vol_multiplier"],
            has_position=float(pos["amount"]) > 0,
            average_entry=pos.get("average_entry", 0),
            open_positions=count_open_positions(),
            strategy_params=self.config.strategy_params(symbol, tf),
        )

    def analyze(self, coin: dict, current_price: float, x_signals=None):
        if not current_price:
            return None
        market = self.build_market_context(coin, current_price)
        strategy = get_strategy(coin)
        return strategy.analyze(coin, market, x_signals)

    def execute_if_needed(self, analysis, coin: dict, current_price: float):
        if analysis is None or analysis.action == "HOLD":
            return None
        if not self.config.virtual_trading:
            return None

        symbol = analysis.symbol
        tf = analysis.timeframe
        if "BUY" in analysis.action:
            order = TradeOrder(
                type="BUY",
                symbol=symbol,
                price=current_price,
                amount=0,
                usdt_amount=self.config.max_usdt_per_trade,
                signal=analysis.action,
            )
            return self.execution.execute(order, tf)

        pos = get_position(symbol, tf)
        from strategies.positions import sell_fraction_for_signal
        fraction = sell_fraction_for_signal(analysis.action)
        amount_sold = float(pos["amount"]) * fraction
        order = TradeOrder(
            type="SELL",
            symbol=symbol,
            price=current_price,
            amount=amount_sold,
            signal=analysis.action,
        )
        return self.execution.execute(order, tf)

    def process_coin(self, coin: dict, current_price: float, x_signals=None) -> str:
        if not current_price:
            return "HOLD"

        analysis = self.analyze(coin, current_price, x_signals)
        if analysis is None:
            return "HOLD"

        trade_result = self.execute_if_needed(analysis, coin, current_price)

        symbol = coin["symbol"]
        tf = analysis.timeframe
        pos = get_position(symbol, tf)
        has_position = float(pos.get("amount", 0)) > 0

        if self.config.raw.get("debug", False):
            print(get_text("debug_ampel_change").format(
                symbol=symbol,
                old=pos.get("last_ampel", "🟡"),
                new=analysis.ampel_emoji,
                old_rsi=pos.get("last_rsi", 45.0),
                new_rsi=analysis.rsi,
                send=analysis.should_notify,
                reason=analysis.notify_reason,
            ))

        if analysis.should_notify and self.notify_callback:
            self.notify_callback(
                analysis.action,
                coin,
                current_price,
                analysis.rsi,
                analysis.lower_bb,
                analysis.vol_multiplier,
                analysis.ampel_emoji,
                analysis.ampel_text,
            )

        pos["last_ampel"] = analysis.ampel_emoji
        pos["last_rsi"] = analysis.rsi

        unrealized = 0.0
        if has_position and pos.get("average_entry", 0) > 0:
            unrealized = (current_price - pos["average_entry"]) * float(pos["amount"])

        history = load_trade_history()
        pos_info = (
            f" | Pos: {float(pos.get('amount', 0)):.2f} | Unrealized: ${unrealized:.1f}"
            if has_position else " | No position"
        )
        executed = f" | Executed: {trade_result.order_type}" if trade_result and trade_result.executed else ""
        print(
            f"{symbol} → {analysis.action} | RSI: {analysis.rsi:.1f} | "
            f"Vol: {analysis.vol_multiplier:.2f}x | Ampel: {analysis.ampel_emoji} {analysis.ampel_text}"
            f"{pos_info}{executed} | Bal: ${history.get('virtual_balance', 0):.0f} | "
            f"RealPnL: ${history.get('realized_pnl', 0):.1f}\n"
        )
        return analysis.action