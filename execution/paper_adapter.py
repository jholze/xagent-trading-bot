from core.models import TradeOrder, TradeResult
from execution.base import ExecutionAdapter
from services.portfolio_service import PortfolioService


class PaperExecutionAdapter(ExecutionAdapter):
    def __init__(self, portfolio: PortfolioService = None):
        self.portfolio = portfolio or PortfolioService()

    @property
    def mode(self) -> str:
        return "paper"

    def execute(self, order: TradeOrder, timeframe: str = "4h") -> TradeResult:
        return self.portfolio.execute_order(order, timeframe)