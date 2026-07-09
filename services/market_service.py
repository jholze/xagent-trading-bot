import threading
import time

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

_24H_BARS = {
    "15m": 96,
    "30m": 48,
    "1h": 24,
    "2h": 12,
    "4h": 6,
    "6h": 4,
    "12h": 2,
    "1d": 1,
}

_exchange_lock = threading.Lock()
_exchanges: dict[str, object] = {}
_funding_lock = threading.Lock()
_funding_cache: dict[str, tuple[float, float]] = {}


def _funding_ttl_sec(config_raw: dict | None = None) -> float:
    arch = ((config_raw or {}).get("architecture") or {})
    return float(arch.get("funding_cache_ttl_sec", 300))


class MarketService:
    """Unified OHLCV and indicator access with multi-exchange fallback."""

    EXCHANGES = ["gate", "binance", "kucoin", "bybit"]
    FUNDING_EXCHANGES = ["gate", "binance", "bybit"]

    def __init__(self, config_raw: dict | None = None):
        self._config_raw = config_raw

    def _arch(self) -> dict:
        if self._config_raw is not None:
            return (self._config_raw.get("architecture") or {})
        try:
            from core.config import get_bot_config

            return get_bot_config().architecture_config
        except Exception:
            return {}

    @classmethod
    def _get_spot_exchange(cls, ex_name: str):
        with _exchange_lock:
            cached = _exchanges.get(f"spot:{ex_name}")
            if cached is not None:
                return cached
            exchange = getattr(ccxt, ex_name)({"enableRateLimit": True, "timeout": 12000})
            _exchanges[f"spot:{ex_name}"] = exchange
            return exchange

    @classmethod
    def _get_swap_exchange(cls, ex_name: str):
        with _exchange_lock:
            cached = _exchanges.get(f"swap:{ex_name}")
            if cached is not None:
                return cached
            exchange = getattr(ccxt, ex_name)(
                {"enableRateLimit": True, "timeout": 12000, "options": {"defaultType": "swap"}}
            )
            _exchanges[f"swap:{ex_name}"] = exchange
            return exchange

    @staticmethod
    def reset_exchange_cache_for_tests() -> None:
        with _exchange_lock:
            _exchanges.clear()
        with _funding_lock:
            _funding_cache.clear()

    @staticmethod
    def _bars_to_dataframe(bars: list) -> pd.DataFrame | None:
        if not bars:
            return None
        df = pd.DataFrame(bars, columns=["ts", "open", "high", "low", "close", "volume"])
        df["rsi"] = talib.RSI(df["close"], timeperiod=14)
        df["upper"], df["middle"], df["lower"] = talib.BBANDS(df["close"], timeperiod=20)
        df["vol_avg"] = df["volume"].rolling(window=20).mean()
        return df

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
        range_24h_pct, change_24h_pct = self._compute_24h_metrics(df, timeframe)
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
            "range_24h_pct": range_24h_pct,
            "change_24h_pct": change_24h_pct,
        }

    @staticmethod
    def _compute_24h_metrics(df: pd.DataFrame, timeframe: str) -> tuple[float | None, float | None]:
        bars = _24H_BARS.get(timeframe, 24)
        if df is None or df.empty or len(df) < bars + 1:
            return None, None
        window = df.tail(bars)
        low = float(window["low"].min())
        high = float(window["high"].max())
        close_now = float(df["close"].iloc[-1])
        close_old = float(df["close"].iloc[-(bars + 1)])
        if close_now <= 0 or close_old <= 0 or low <= 0:
            return None, None
        range_pct = (high - low) / close_now * 100.0
        change_pct = (close_now / close_old - 1.0) * 100.0
        return float(range_pct), float(change_pct)

    def fetch_funding_rate(self, symbol: str) -> float | None:
        """Return perpetual funding rate in percent (e.g. -0.04 = -0.04%)."""
        now = time.time()
        ttl = _funding_ttl_sec(self._config_raw)
        with _funding_lock:
            cached = _funding_cache.get(symbol)
            if cached and now - cached[1] <= ttl:
                return cached[0]

        base = symbol.split("/")[0]
        swap_symbol = f"{base}/USDT:USDT"
        for ex_name in self.FUNDING_EXCHANGES:
            try:
                exchange = self._get_swap_exchange(ex_name)
                if not exchange.has.get("fetchFundingRate"):
                    continue
                data = exchange.fetch_funding_rate(swap_symbol)
                rate = data.get("fundingRate")
                if rate is None:
                    continue
                value = float(rate) * 100.0
                with _funding_lock:
                    _funding_cache[symbol] = (value, now)
                return value
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

    def infer_ohlcv_peak_price(
        self,
        symbol: str,
        timeframe: str,
        since_iso: str | None = None,
        *,
        limit: int = 200,
    ) -> float | None:
        """Highest candle high since *since_iso* (or full window if unknown)."""
        from datetime import datetime

        df = self._fetch_ohlcv(symbol, timeframe, limit)
        if df is None or df.empty:
            return None
        if since_iso:
            try:
                since_dt = datetime.fromisoformat(str(since_iso).replace("Z", ""))
                since_ms = since_dt.timestamp() * 1000
                df = df[df["ts"] >= since_ms]
                if df.empty:
                    return None
            except Exception:
                pass
        return float(df["high"].max())

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

    @staticmethod
    def compute_exit_metrics_15m(
        df: pd.DataFrame,
        *,
        ema_period: int = 20,
        vol_avg_period: int = 20,
    ) -> dict | None:
        """15m metrics for P1 exit sensor (weakness, climax, pullback)."""
        base = MarketService.compute_15m_sensor_metrics(
            df, ema_period=ema_period, vol_avg_period=vol_avg_period
        )
        if not base:
            return None

        ema = talib.EMA(df["close"], timeperiod=ema_period)
        if pd.isna(ema.iloc[-1]):
            return None

        highs = df["high"]
        lower_high = False
        if len(highs) >= 4:
            swing = float(highs.iloc[-3])
            last_high = float(highs.iloc[-1])
            prev_peak = float(highs.iloc[-4:-1].max())
            lower_high = swing >= prev_peak * 0.995 and last_high < swing * 0.998

        close_val = float(df["close"].iloc[-1])
        ema_val = float(ema.iloc[-1])
        open_val = float(df["open"].iloc[-1])
        high_val = float(df["high"].iloc[-1])
        low_val = float(df["low"].iloc[-1])
        candle_range = high_val - low_val
        upper_body = max(open_val, close_val)
        upper_wick_pct = (
            ((high_val - upper_body) / candle_range) * 100.0 if candle_range > 0 else 0.0
        )

        volume = float(df["volume"].iloc[-1])
        vol_avg = float(df["volume"].rolling(window=vol_avg_period).mean().iloc[-1])
        vol_above_avg = volume > vol_avg if vol_avg > 0 else False

        base.update({
            "ema": ema_val,
            "close_below_ema": close_val < ema_val,
            "lower_high": lower_high,
            "upper_wick_pct": float(upper_wick_pct),
            "vol_above_avg": bool(vol_above_avg),
        })
        return base

    @staticmethod
    def compute_exit_metrics_1h(df: pd.DataFrame) -> dict | None:
        if df is None or df.empty or len(df) < 20:
            return None
        rsi = talib.RSI(df["close"], timeperiod=14)
        if pd.isna(rsi.iloc[-1]):
            return None
        rsi_cur = float(rsi.iloc[-1])
        rsi_peak_5 = float(rsi.tail(5).max())
        peak_min = 70.0
        current_max = 60.0
        rsi_rollover = rsi_peak_5 >= peak_min and rsi_cur < current_max
        return {
            "rsi": rsi_cur,
            "rsi_peak_5": rsi_peak_5,
            "rsi_rollover": bool(rsi_rollover),
        }

    def fetch_exit_metrics_15m(self, symbol: str, cfg: dict | None = None) -> dict | None:
        cfg = cfg or {}
        vol_avg_period = int(cfg.get("vol_avg_period", 20))
        ema_period = int((cfg.get("weakness_15m") or {}).get("ema_period", 20))
        limit = max(vol_avg_period, ema_period) + 30
        df = self._fetch_ohlcv(symbol, "15m", limit)
        return self.compute_exit_metrics_15m(
            df, ema_period=ema_period, vol_avg_period=vol_avg_period
        )

    def fetch_exit_metrics_1h(self, symbol: str, *, limit: int = 40) -> dict | None:
        df = self._fetch_ohlcv(symbol, "1h", limit)
        return self.compute_exit_metrics_1h(df)

    def btc_relative_return_delta(
        self,
        symbol: str,
        timeframe: str = "4h",
        periods: int = 1,
    ) -> float | None:
        """Coin % change minus BTC % change over `periods` bars."""
        if symbol.upper().startswith("BTC/"):
            return None
        limit = periods + 5
        coin_df = self._fetch_ohlcv(symbol, timeframe, limit)
        btc_df = self._fetch_ohlcv("BTC/USDT", timeframe, limit)
        if coin_df is None or btc_df is None:
            return None
        coin_chg = self._pct_change(coin_df, periods)
        btc_chg = self._pct_change(btc_df, periods)
        if coin_chg is None or btc_chg is None:
            return None
        return coin_chg - btc_chg

    def _fetch_ohlcv(self, symbol: str, timeframe: str, limit: int):
        cache = None
        try:
            from bus.ohlcv_cache import ohlcv_cache_enabled, ohlcv_cache_from_config

            if ohlcv_cache_enabled(self._config_raw):
                cache = ohlcv_cache_from_config(self._config_raw)
                cached = cache.get(symbol, timeframe, limit)
                if cached and cached.bars:
                    return self._bars_to_dataframe(cached.bars)
        except Exception:
            cache = None

        for ex_name in self.EXCHANGES:
            try:
                exchange = self._get_spot_exchange(ex_name)
                bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
                if cache and bars:
                    cache.set(symbol, timeframe, limit, bars, exchange=ex_name)
                return self._bars_to_dataframe(bars)
            except Exception as e:
                log(f"{ex_name.capitalize()} fetch failed for {symbol}: {e}", "WARNING")
        return None


def ohlcv_cache_stats() -> dict:
    try:
        from bus.ohlcv_cache import ohlcv_cache_from_config, ohlcv_cache_enabled

        if not ohlcv_cache_enabled():
            return {}
        return ohlcv_cache_from_config().stats()
    except Exception:
        return {}


def reset_ohlcv_cache_cycle_stats() -> None:
    try:
        from bus.ohlcv_cache import ohlcv_cache_from_config

        ohlcv_cache_from_config().reset_stats()
    except Exception:
        pass