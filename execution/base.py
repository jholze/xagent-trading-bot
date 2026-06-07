from abc import ABC, abstractmethod

from core.models import TradeOrder, TradeResult


class ExecutionAdapter(ABC):
    @abstractmethod
    def execute(self, order: TradeOrder, timeframe: str = "4h") -> TradeResult:
        ...

    @property
    @abstractmethod
    def mode(self) -> str:
        ...