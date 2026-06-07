from abc import ABC, abstractmethod

from core.models import MarketContext, SignalAnalysis


class BaseStrategy(ABC):
    name: str = "base"

    @abstractmethod
    def analyze(self, coin: dict, market: MarketContext, x_signals=None) -> SignalAnalysis:
        ...

    def get_timeframe(self, coin: dict) -> str:
        return coin.get("timeframe", "4h")