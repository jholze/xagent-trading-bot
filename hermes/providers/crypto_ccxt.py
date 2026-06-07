from hermes.backtester import Backtester
from hermes.providers.base import DataProvider


class CryptoCcxtProvider(DataProvider):
    """Crypto OHLCV via ccxt (wraps Backtester fetch logic)."""

    def __init__(self):
        self._backtester = Backtester()

    def fetch_ohlcv(self, symbol: str, timeframe: str, days: int) -> list:
        df = self._backtester._fetch_ohlcv(symbol, timeframe, days)
        if df is None or df.empty:
            return []
        return df.values.tolist()

    def fetch_price(self, symbol: str) -> float | None:
        df = self._backtester._fetch_ohlcv(symbol, "1h", 1)
        if df is None or df.empty:
            return None
        return float(df.iloc[-1]["close"])