import ccxt
import pandas as pd
import talib

from logger import log

_TF_HOURS = {
    "15m": 0.25,
    "30m": 0.5,
    "1h": 1.0,
    "2h": 2.0,
    "4h": 4.0,
    "6h": 6.0,
    "12h": 12.0,
    "1d": 24.0,
}


class MarketService:
    """Unified OHLCV and indicator access with multi-exchange fallback."""

    EXCHANGES = ["gate", "binance", "kucoin", "bybit"]
    FUNDING_EXCHANGES = ["gate", "binance", "bybit"]

    def fetch_indicators(self, symbol: str, timeframe: str, current_price: float, limit: int = 100) -> dict:
        df = self._fetch_ohlcv(symbol, timeframe, limit)
        if df is None or df.empty:
            log(f"All exchanges failed for {symbol}. Using fallback data.", "ERROR")
            return {
                "rsi": 45.0,
                "lower_bb": current_price * 0.97,
                "middle_bb": current_price,
                "upper_bb": current_price * 1.03,
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
        if "upper" not in df.columns:
            upper, middle, lower = talib.BBANDS(df["close"], timeperiod=20)
            df["upper"], df["middle"], df["lower"] = upper, middle, lower
        lower_bb = float(df["lower"].iloc[-1]) if pd.notna(df["lower"].iloc[-1]) else close * 0.97
        middle_bb = float(df["middle"].iloc[-1]) if pd.notna(df["middle"].iloc[-1]) else close
        upper_bb = float(df["upper"].iloc[-1]) if pd.notna(df["upper"].iloc[-1]) else close * 1.03
        return {
            "rsi": float(df["rsi"].iloc[-1]),
            "lower_bb": lower_bb,
            "middle_bb": middle_bb,
            "upper_bb": upper_bb,
            "vol_multiplier": float(vol_multiplier),
            "atr": atr,
            "atr_pct": float(atr_pct),
        }

    def fetch_funding_rate(self, symbol: str) -> float | None:
        """Return perpetual funding rate in percent (e.g. -0.04 = -0.04%)."""
        base = symbol.split("/")[0]
        swap_symbol = f"{base}/USDT:USDT"
        for ex_name in self.FUNDING_EXCHANGES:
            try:
                exchange = getattr(ccxt, ex_name)(
                    {"enableRateLimit": True, "timeout": 12000, "options": {"defaultType": "swap"}}
                )
                if not exchange.has.get("fetchFundingRate"):
                    continue
                data = exchange.fetch_funding_rate(swap_symbol)
                rate = data.get("fundingRate")
                if rate is None:
                    continue
                return float(rate) * 100.0
            except Exception as e:
                log(f"{ex_name.capitalize()} funding fetch failed for {symbol}: {e}", "WARNING")
        return None

    def btc_underperformance_ratio(
        self,
        symbol: str,
        timeframe: str,
        lookback_hours: float = 8.0,
    ) -> float | None:
        """
        Return how much worse the coin performed vs BTC over lookback_hours.

        Example: BTC -2%, coin -5% → ratio 2.5.
        """
        if symbol.upper().startswith("BTC/"):
            return None
        tf_hours = _TF_HOURS.get(timeframe, 1.0)
        periods = max(2, int(lookback_hours / tf_hours))
        limit = periods + 5
        coin_df = self._fetch_ohlcv(symbol, timeframe, limit)
        btc_df = self._fetch_ohlcv("BTC/USDT", timeframe, limit)
        if coin_df is None or btc_df is None or len(coin_df) < periods + 1 or len(btc_df) < periods + 1:
            return None

        coin_chg = self._pct_change(coin_df, periods)
        btc_chg = self._pct_change(btc_df, periods)
        if coin_chg is None or btc_chg is None:
            return None
        if coin_chg >= btc_chg:
            return None
        coin_drop = abs(coin_chg)
        btc_drop = abs(btc_chg) if btc_chg < 0 else max(abs(btc_chg), 0.5)
        if btc_drop <= 0:
            return coin_drop
        return coin_drop / btc_drop

    @staticmethod
    def _pct_change(df: pd.DataFrame, periods: int) -> float | None:
        if len(df) < periods + 1:
            return None
        old = float(df["close"].iloc[-(periods + 1)])
        new = float(df["close"].iloc[-1])
        if old <= 0:
            return None
        return (new / old - 1.0) * 100.0

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 100):
        """Public OHLCV fetch (same pipeline as fetch_indicators)."""
        return self._fetch_ohlcv(symbol, timeframe, limit)

    @staticmethod
    def compute_15m_sensor_metrics(
        df: pd.DataFrame,
        *,
        ema_period: int = 9,
        vol_avg_period: int = 20,
    ) -> dict | None:
        """Derive 15m volume/movement metrics from OHLCV (no network)."""
        if df is None or df.empty or len(df) < vol_avg_period + 2:
            return None
        close = df["close"]
        ema = talib.EMA(close, timeperiod=ema_period)
        vol_avg = df["volume"].rolling(window=vol_avg_period).mean()
        atr = talib.ATR(df["high"], df["low"], close, timeperiod=14)
        if pd.isna(ema.iloc[-1]) or pd.isna(vol_avg.iloc[-1]) or pd.isna(atr.iloc[-1]):
            return None

        volume = float(df["volume"].iloc[-1])
        vol_avg_val = float(vol_avg.iloc[-1])
        volume_spike_ratio = volume / vol_avg_val if vol_avg_val > 0 else 0.0

        ema_cur = float(ema.iloc[-1])
        ema_prev = float(ema.iloc[-2])
        close_val = float(close.iloc[-1])
        close_prev = float(close.iloc[-2])
        price_momentum = close_val > ema_cur and close_prev <= ema_prev

        atr_val = float(atr.iloc[-1])
        open_val = float(df["open"].iloc[-1])
        body = abs(close_val - open_val)
        body_atr_ratio = body / atr_val if atr_val > 0 else 0.0
        swing_low_5 = float(df["low"].tail(5).min())

        return {
            "volume_spike_ratio": float(volume_spike_ratio),
            "ema9": ema_cur,
            "ema_prev": ema_prev,
            "price_momentum": bool(price_momentum),
            "body_atr_ratio": float(body_atr_ratio),
            "atr_15m": atr_val,
            "swing_low_5": swing_low_5,
            "close": close_val,
        }

    def fetch_15m_sensor_metrics(self, symbol: str, cfg: dict | None = None) -> dict | None:
        cfg = cfg or {}
        vol_avg_period = int(cfg.get("vol_avg_period", 20))
        ema_period = int(cfg.get("ema_period", 9))
        limit = vol_avg_period + 30
        df = self._fetch_ohlcv(symbol, "15m", limit)
        return self.compute_15m_sensor_metrics(
            df, ema_period=ema_period, vol_avg_period=vol_avg_period
        )

    def _fetch_ohlcv(self, symbol: str, timeframe: str, limit: int):
        for ex_name in self.EXCHANGES:
            try:
                exchange = getattr(ccxt, ex_name)({"enableRateLimit": True, "timeout": 12000})
                bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
                df = pd.DataFrame(bars, columns=["ts", "open", "high", "low", "close", "volume"])
                df["rsi"] = talib.RSI(df["close"], timeperiod=14)
                df["upper"], df["middle"], df["lower"] = talib.BBANDS(df["close"], timeperiod=20)
                df["vol_avg"] = df["volume"].rolling(window=20).mean()
                return df
            except Exception as e:
                log(f"{ex_name.capitalize()} fetch failed for {symbol}: {e}", "WARNING")
        return None