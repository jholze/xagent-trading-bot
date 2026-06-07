import ccxt
import pandas as pd
import talib

from logger import log


class MarketService:
    """Unified OHLCV and indicator access with multi-exchange fallback."""

    EXCHANGES = ["gate", "binance", "kucoin", "bybit"]

    def fetch_indicators(self, symbol: str, timeframe: str, current_price: float, limit: int = 100) -> dict:
        df = self._fetch_ohlcv(symbol, timeframe, limit)
        if df is None or df.empty:
            log(f"All exchanges failed for {symbol}. Using fallback data.", "ERROR")
            return {
                "rsi": 45.0,
                "lower_bb": current_price * 0.97,
                "vol_multiplier": 1.3,
                "atr": current_price * 0.03,
                "atr_pct": 3.0,
            }

        recent_vol_avg = df["volume"].tail(4).mean()
        long_vol_avg = df["vol_avg"].iloc[-1]
        vol_multiplier = recent_vol_avg / long_vol_avg if long_vol_avg and long_vol_avg > 0 else 1.0
        close = float(df["close"].iloc[-1])
        atr = float(talib.ATR(df["high"], df["low"], df["close"], timeperiod=14).iloc[-1])
        atr_pct = (atr / close * 100.0) if close > 0 else 3.0
        return {
            "rsi": float(df["rsi"].iloc[-1]),
            "lower_bb": float(df["lower"].iloc[-1]),
            "vol_multiplier": float(vol_multiplier),
            "atr": atr,
            "atr_pct": float(atr_pct),
        }

    def _fetch_ohlcv(self, symbol: str, timeframe: str, limit: int):
        for ex_name in self.EXCHANGES:
            try:
                exchange = getattr(ccxt, ex_name)({"enableRateLimit": True, "timeout": 12000})
                bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
                df = pd.DataFrame(bars, columns=["ts", "open", "high", "low", "close", "volume"])
                df["rsi"] = talib.RSI(df["close"], timeperiod=14)
                _, _, df["lower"] = talib.BBANDS(df["close"], timeperiod=20)
                df["vol_avg"] = df["volume"].rolling(window=20).mean()
                return df
            except Exception as e:
                log(f"{ex_name.capitalize()} fetch failed for {symbol}: {e}", "WARNING")
        return None