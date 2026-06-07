from core.models import TradeOrder, TradeResult
from execution.base import ExecutionAdapter
from logger import log


class GateExecutionAdapter(ExecutionAdapter):
    """Gate.io live execution stub — Phase 4 will implement create_order."""

    def __init__(self):
        self._exchange = None

    @property
    def mode(self) -> str:
        return "live"

    def execute(self, order: TradeOrder, timeframe: str = "4h") -> TradeResult:
        log(f"Gate live execution not yet enabled for {order.type} {order.symbol}", "WARNING")
        return TradeResult(
            executed=False,
            order_type=order.type,
            symbol=order.symbol,
            message="Live Gate.io execution is not enabled yet (Phase 4)",
        )