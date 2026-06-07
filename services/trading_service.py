from core.config import BotConfig, get_bot_config
from core.models import TradeOrder, TradeResult
from execution.factory import get_execution_adapter
from logger import log
from risk.risk_manager import RiskManager
from services.market_service import MarketService
from services.portfolio_service import PortfolioService


class TradingService:
    """Unified trading facade — respects trading_mode, risk limits, and safety gates."""

    def __init__(
        self,
        config: BotConfig = None,
        portfolio: PortfolioService = None,
        risk_manager: RiskManager = None,
        market_service: MarketService = None,
    ):
        self.config = config or get_bot_config()
        self.portfolio = portfolio or PortfolioService(self.config)
        self.market = market_service or MarketService()
        self.risk = risk_manager or RiskManager(self.config, self.portfolio, self.market)

    def refresh(self):
        self.config.refresh()
        return self

    @property
    def adapter(self):
        return get_execution_adapter(self.config, self.portfolio)

    def mode_label(self) -> str:
        mode = self.config.trading_mode
        if mode == "live":
            confirmed = "CONFIRMED" if self.config.live_confirmed else "needs /live_confirm"
            dry = " [DRY RUN]" if self.config.live_config.get("dry_run", True) else ""
            return f"live ({confirmed}){dry}"
        if mode == "off":
            return "off (analysis only)"
        return "paper"

    def can_execute(self, source: str = "auto", trust_score: float = None) -> tuple:
        mode = self.config.trading_mode
        if mode == "off":
            return False, "Trading disabled (mode=off). Use /mode paper to enable."
        if mode == "paper":
            return True, ""
        if mode == "live":
            if not self.config.live_confirmed:
                return False, "Live trading requires /live_confirm first."
            min_trust = self.config.live_config.get("require_min_trust_score", 70)
            if source == "x" and trust_score is not None and trust_score < min_trust:
                return False, f"Trust score {trust_score:.0f} below live minimum ({min_trust})."
            return True, ""
        return False, f"Unknown trading mode: {mode}"

    def max_usdt_for_order(self) -> float:
        if self.config.trading_mode == "live":
            return float(self.config.live_config.get("max_usdt_per_trade", self.config.max_usdt_per_trade))
        return self.config.max_usdt_per_trade

    def evaluate_risk(
        self,
        order: TradeOrder,
        timeframe: str = "4h",
        source: str = "manual",
        trust_score: float = None,
        confidence: float = None,
        indicators: dict = None,
    ):
        return self.risk.evaluate(
            order,
            timeframe,
            source=source,
            trust_score=trust_score,
            confidence=confidence,
            indicators=indicators,
        )

    def execute_order(
        self,
        order: TradeOrder,
        timeframe: str = "4h",
        source: str = "manual",
        trust_score: float = None,
        confidence: float = None,
        indicators: dict = None,
    ) -> TradeResult:
        ok, reason = self.can_execute(source=source, trust_score=trust_score)
        if not ok:
            log(f"Trade blocked: {reason}", "WARNING")
            return TradeResult(False, order.type, order.symbol, message=reason)

        decision = self.risk.evaluate(
            order,
            timeframe,
            source=source,
            trust_score=trust_score,
            confidence=confidence,
            indicators=indicators,
        )
        if not decision.approved:
            log(f"Risk rejected {order.type} {order.symbol}: {decision.message}", "WARNING")
            return TradeResult(False, order.type, order.symbol, message=decision.message)

        approved_order = decision.order
        result = self.adapter.execute(approved_order, timeframe)
        if result.executed:
            log(
                f"{self.adapter.mode.upper()} {approved_order.type} {approved_order.symbol} "
                f"executed (${approved_order.usdt_amount:.0f})",
                "INFO",
            )
        elif decision.size_multiplier != 1.0 and not result.message:
            result.message = f"Size multiplier: {decision.size_multiplier:.2f}x"
        return result

    def execute_buy(self, symbol: str, timeframe: str, price: float, usdt: float = None) -> TradeResult:
        order = TradeOrder(
            type="BUY",
            symbol=symbol,
            price=price,
            amount=0,
            usdt_amount=usdt or 0,
        )
        return self.execute_order(order, timeframe, source="manual")

    def execute_sell(self, symbol: str, timeframe: str, price: float, signal: str, amount: float) -> TradeResult:
        order = TradeOrder(type="SELL", symbol=symbol, price=price, amount=amount, signal=signal)
        return self.execute_order(order, timeframe, source="manual")