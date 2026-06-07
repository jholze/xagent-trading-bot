from abc import ABC, abstractmethod


class DataProvider(ABC):
    """Abstract market data provider for future Bittensor/stock adapters."""

    @abstractmethod
    def fetch_ohlcv(self, symbol: str, timeframe: str, days: int) -> list:
        ...

    @abstractmethod
    def fetch_price(self, symbol: str) -> float | None:
        ...