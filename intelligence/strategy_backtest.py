"""Historical strategy backtest for per-coin RSI/BB parameter tuning."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional
from zoneinfo import ZoneInfo

import ccxt
import numpy as np
import pandas as pd
import talib

from logger import log

_OHLCV_CACHE: dict[tuple, tuple[float, list]] = {}
_CACHE_TTL = 86400


@dataclass
class VolumeProfile:
    us_open_spike: float = 1.0
    us_close_spike: float = 1.0
    us_session_volume_ratio: float = 0.0
    dominant_session: str = "mixed"

    def to_dict(self) -> dict:
        return {
            "us_open_spike": round(self.us_open_spike, 2),
            "us_close_spike": round(self.us_close_spike, 2),
            "us_session_volume_ratio": round(self.us_session_volume_ratio, 2),
            "dominant_session": self.dominant_session,
        }


@dataclass
class SimulationMetrics:
    buy_signals: int = 0
    sell_signals: int = 0
    signal_churn: int = 0
    pnl_sim: float = 0.0
    win_rate: float = 0.0
    atr_pct: float = 0.0
    volume_profile: VolumeProfile = field(default_factory=VolumeProfile)

    def to_dict(self) -> dict:
        data = {
            "buy_signals": self.buy_signals,
            "sell_signals": self.sell_signals,
            "signal_churn": self.signal_churn,
            "pnl_sim": round(self.pnl_sim, 2),
            "win_rate": round(self.win_rate, 3),
            "atr_pct": round(self.atr_pct, 2),
        }
        data.update(self.volume_profile.to_dict())
        return data


@dataclass
class BacktestRunResult:
    symbol: str
    timeframe: str
    days: int
    params: dict
    metrics: SimulationMetrics
    best_variant: Optional[dict] = None
    improvement_pct: float = 0.0

    @property
    def coin_key(self) -> str:
        return f"{self.symbol}_{self.timeframe}"


def coin_key(symbol: str, timeframe: str) -> str:
    return f"{symbol}_{timeframe}"


def classify_coin(symbol: str, strategy_entry: dict = None) -> str:
    desc = (strategy_entry or {}).get("description", "").lower()
    sym = symbol.split("/")[0].upper()
    if any(k in desc for k in ("meme", "high-vol", "high vol")):
        return "meme"
    if sym in ("BTC", "ETH", "SOL"):
        return "large_cap"
    if any(k in desc for k in ("large-cap", "large cap")):
        return "large_cap"
    if any(k in desc for k in ("mid-cap", "mid cap")):
        return "mid_cap"
    if sym in ("ARIA", "RAVE", "PEPE", "DOGE", "WIF", "BONK"):
        return "meme"
    return "mid_cap"


class StrategyBacktester:
    def __init__(self, config: dict = None, ohlcv_fetcher: Callable = None):
        from core.config import get_bot_config

        cfg = config or get_bot_config().raw
        self.cfg = cfg
        self.bt_cfg = cfg.get("strategy_backtest", {})
        self.slippage = float(cfg.get("slippage_percent", 1.5)) / 100.0
        self._fetch = ohlcv_fetcher or self._default_fetch_ohlcv

    def run(self, symbol: str, timeframe: str, params: dict, days: int = None) -> BacktestRunResult:
        days = days or int(self.bt_cfg.get("days", 30))
        df = self._prepare_df(symbol, timeframe, days)
        metrics = self._simulate(df, params)
        return BacktestRunResult(symbol=symbol, timeframe=timeframe, days=days, params=dict(params), metrics=metrics)

    def compare_variants(
        self,
        symbol: str,
        timeframe: str,
        base_params: dict,
        days: int = None,
    ) -> BacktestRunResult:
        days = days or int(self.bt_cfg.get("days", 30))
        df = self._prepare_df(symbol, timeframe, days)
        current = self._simulate(df, base_params)
        best_params = dict(base_params)
        best_metrics = current
        variants = self._variant_grid(base_params)

        for variant in variants:
            if variant == base_params:
                continue
            trial = self._simulate(df, variant)
            if self._score(trial) > self._score(best_metrics):
                best_params = variant
                best_metrics = trial

        base_score = self._score(current)
        best_score = self._score(best_metrics)
        improvement = 0.0
        if base_score > 0:
            improvement = ((best_score - base_score) / base_score) * 100.0
        elif best_score > base_score:
            improvement = 100.0

        return BacktestRunResult(
            symbol=symbol,
            timeframe=timeframe,
            days=days,
            params=dict(base_params),
            metrics=current,
            best_variant=best_params if best_params != base_params else None,
            improvement_pct=round(improvement, 2),
        )

    def _score(self, metrics: SimulationMetrics) -> float:
        churn_penalty = max(0, metrics.signal_churn - 12) * 2
        return metrics.pnl_sim + metrics.win_rate * 50 - churn_penalty

    def _variant_grid(self, params: dict) -> list[dict]:
        base = dict(params)
        rsi_low = int(base.get("rsi_buy_low", 28))
        rsi_high = int(base.get("rsi_buy_high", 45))
        vol = float(base.get("volume_multiplier", 1.3))
        variants = [base]
        for dlow, dhigh, dvol in (
            (-3, 0, -0.2),
            (0, 3, 0),
            (3, 0, 0.2),
            (0, -3, -0.1),
            (0, 3, 0.1),
        ):
            v = dict(base)
            v["rsi_buy_low"] = max(20, rsi_low + dlow)
            v["rsi_buy_high"] = min(60, rsi_high + dhigh)
            v["volume_multiplier"] = round(max(1.0, min(2.0, vol + dvol)), 2)
            variants.append(v)
        unique = []
        seen = set()
        for v in variants:
            key = (v.get("rsi_buy_low"), v.get("rsi_buy_high"), v.get("volume_multiplier"))
            if key not in seen:
                seen.add(key)
                unique.append(v)
        return unique

    def _prepare_df(self, symbol: str, timeframe: str, days: int) -> pd.DataFrame:
        bars = self._fetch(symbol, timeframe, days)
        if not bars:
            raise ValueError(f"No OHLCV data for {symbol} {timeframe}")
        df = pd.DataFrame(bars, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        return df.set_index("timestamp")

    def _default_fetch_ohlcv(self, symbol: str, timeframe: str, days: int) -> list:
        key = (symbol, timeframe, days)
        now = time.time()
        if key in _OHLCV_CACHE:
            cached_at, data = _OHLCV_CACHE[key]
            if now - cached_at < _CACHE_TTL:
                return data
        since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
        exchange = ccxt.gate({"enableRateLimit": True})
        try:
            bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=1000)
            _OHLCV_CACHE[key] = (now, bars)
            return bars
        except Exception as e:
            log(f"Strategy backtest OHLCV fetch failed for {symbol}: {e}", "WARNING")
            return []

    def _simulate(self, df: pd.DataFrame, params: dict) -> SimulationMetrics:
        if len(df) < 25:
            return SimulationMetrics()

        close = df["close"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        volume = df["volume"].astype(float)

        rsi = talib.RSI(close.values, timeperiod=14)
        upper, middle, lower = talib.BBANDS(close.values, timeperiod=20)
        vol_avg = volume.rolling(window=20).mean()
        atr = talib.ATR(high.values, low.values, close.values, timeperiod=14)

        rsi_buy_low = float(params.get("rsi_buy_low", 28))
        rsi_buy_high = float(params.get("rsi_buy_high", 45))
        vol_mult_min = float(params.get("volume_multiplier", 1.3))
        rsi_sell_30 = float(params.get("rsi_sell_30", 70))
        stop_loss_pct = float(params.get("stop_loss_pct", 50))

        buys = sells = 0
        in_position = False
        entry_price = 0.0
        wins = trades = 0
        pnl = 0.0
        usdt_per_trade = 100.0

        for i in range(20, len(df)):
            price = float(close.iloc[i])
            r = float(rsi[i]) if not np.isnan(rsi[i]) else 50.0
            lo = float(lower[i]) if not np.isnan(lower[i]) else price
            vma = float(vol_avg.iloc[i]) if not np.isnan(vol_avg.iloc[i]) else float(volume.iloc[i])
            vol_m = float(volume.iloc[i]) / vma if vma > 0 else 0

            if not in_position:
                if price <= lo * 1.01 and rsi_buy_low <= r <= rsi_buy_high and vol_m >= vol_mult_min:
                    buys += 1
                    in_position = True
                    entry_price = price * (1 + self.slippage)
            else:
                loss_pct = (price / entry_price - 1) * -100 if entry_price > 0 else 0
                if loss_pct > stop_loss_pct or r >= rsi_sell_30:
                    sells += 1
                    exit_price = price * (1 - self.slippage)
                    trade_pnl = (exit_price - entry_price) / entry_price * usdt_per_trade
                    pnl += trade_pnl
                    trades += 1
                    if trade_pnl > 0:
                        wins += 1
                    in_position = False
                    entry_price = 0.0

        atr_val = float(atr[-1]) if len(atr) and not np.isnan(atr[-1]) else 0
        last_price = float(close.iloc[-1]) or 1.0
        atr_pct = (atr_val / last_price) * 100 if last_price > 0 else 0
        vol_profile = detect_volume_profile(df, self.bt_cfg.get("us_market", {}))

        return SimulationMetrics(
            buy_signals=buys,
            sell_signals=sells,
            signal_churn=buys + sells,
            pnl_sim=round(pnl, 2),
            win_rate=round(wins / trades, 3) if trades else 0.0,
            atr_pct=atr_pct,
            volume_profile=vol_profile,
        )


def detect_volume_profile(df: pd.DataFrame, us_cfg: dict) -> VolumeProfile:
    if df.empty or not us_cfg.get("enabled", True):
        return VolumeProfile()

    tz_name = us_cfg.get("timezone", "Europe/Berlin")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")

    open_parts = str(us_cfg.get("open", "15:30")).split(":")
    close_parts = str(us_cfg.get("close", "22:00")).split(":")
    open_h, open_m = int(open_parts[0]), int(open_parts[1])
    close_h, close_m = int(close_parts[0]), int(close_parts[1])

    volumes = df["volume"].astype(float)
    if volumes.sum() <= 0:
        return VolumeProfile()

    us_mask = []
    off_mask = []
    for ts in df.index:
        local = ts.tz_convert(tz)
        minutes = local.hour * 60 + local.minute
        open_min = open_h * 60 + open_m
        close_min = close_h * 60 + close_m
        in_us = open_min <= minutes <= close_min
        us_mask.append(in_us)
        off_mask.append(not in_us)

    us_vol = volumes[us_mask].mean() if any(us_mask) else 0
    off_vol = volumes[off_mask].mean() if any(off_mask) else 0
    baseline = off_vol if off_vol > 0 else volumes.mean()
    us_ratio = float(us_vol / volumes.mean()) if volumes.mean() > 0 else 0

    open_window = []
    close_window = []
    for ts, vol in zip(df.index, volumes):
        local = ts.tz_convert(tz)
        minutes = local.hour * 60 + local.minute
        if open_min <= minutes <= open_min + 150:
            open_window.append(vol)
        if close_min - 60 <= minutes <= close_min + 60:
            close_window.append(vol)

    open_spike = float(np.mean(open_window) / baseline) if open_window and baseline > 0 else 1.0
    close_spike = float(np.mean(close_window) / baseline) if close_window and baseline > 0 else 1.0
    dominant = "us" if us_ratio > 0.55 else "mixed"

    return VolumeProfile(
        us_open_spike=open_spike,
        us_close_spike=close_spike,
        us_session_volume_ratio=us_ratio,
        dominant_session=dominant,
    )