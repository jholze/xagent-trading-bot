import numpy as np
import pandas as pd

from services.market_service import MarketService


def _sample_15m_df(rows: int = 30, spike_last: bool = False) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    close = 100 + np.cumsum(rng.normal(0, 0.5, rows))
    volume = rng.uniform(1000, 2000, rows)
    if spike_last:
        volume[-1] = 6000
    high = close + rng.uniform(0.1, 0.5, rows)
    low = close - rng.uniform(0.1, 0.5, rows)
    open_ = close + rng.normal(0, 0.2, rows)
    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    )


class TestMarketService15m:
    def test_compute_15m_sensor_metrics_returns_expected_keys(self):
        df = _sample_15m_df(30, spike_last=True)
        metrics = MarketService.compute_15m_sensor_metrics(df)
        assert metrics is not None
        assert metrics["volume_spike_ratio"] > 1.0
        assert "ema9" in metrics
        assert "body_atr_ratio" in metrics
        assert "atr_15m" in metrics
        assert "swing_low_5" in metrics

    def test_compute_15m_insufficient_rows_returns_none(self):
        df = _sample_15m_df(5)
        assert MarketService.compute_15m_sensor_metrics(df) is None

    def test_fetch_ohlcv_delegates_to_internal(self, monkeypatch):
        ms = MarketService()
        captured = {}

        def fake_fetch(symbol, timeframe, limit):
            captured["args"] = (symbol, timeframe, limit)
            return _sample_15m_df(30)

        monkeypatch.setattr(ms, "_fetch_ohlcv", fake_fetch)
        out = ms.fetch_ohlcv("VELVET/USDT", "15m", 40)
        assert captured["args"] == ("VELVET/USDT", "15m", 40)
        assert out is not None

    def test_fetch_indicators_4h_unchanged_with_monkeypatch(self, monkeypatch):
        ms = MarketService()
        df = _sample_15m_df(50)
        df["rsi"] = 48.0
        df["upper"] = df["close"] * 1.02
        df["middle"] = df["close"]
        df["lower"] = df["close"] * 0.98
        df["vol_avg"] = df["volume"].rolling(20).mean()

        monkeypatch.setattr(ms, "_fetch_ohlcv", lambda *a, **k: df)
        ind = ms.fetch_indicators("BTC/USDT", "4h", 100.0)
        assert 40 < ind["rsi"] < 55
        assert ind["atr_pct"] > 0