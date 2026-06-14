from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from hermes.counterfactual import (
    CounterfactualResult,
    build_seed_from_trades,
    compare_params_window,
)
from hermes.pipeline_backtest import PipelineBacktester


NOW = datetime(2026, 6, 14, 12, 0, 0, tzinfo=timezone.utc)
WINDOW_START = datetime(2026, 6, 13, 0, 0, 0, tzinfo=timezone.utc)


def _synthetic_ohlcv():
    """50 bars ending at NOW; pump in the live window for take-profit."""
    end_ms = int(NOW.timestamp() * 1000)
    n = 50
    close = np.full(n, 0.28)
    close[42:] = 0.42
    ts = [end_ms - (n - 1 - i) * 4 * 3600 * 1000 for i in range(n)]
    return pd.DataFrame({
        "ts": ts,
        "open": close,
        "high": close + 0.02,
        "low": close - 0.02,
        "close": close,
        "volume": np.full(n, 8000.0),
    })


def test_build_seed_from_manual_buy():
    trades = [
        {
            "type": "BUY",
            "symbol": "H/USDT",
            "timestamp": "2026-06-13T15:16:07.635908",
            "price": 0.27867,
            "usdt_amount": 250.0,
            "amount": 0,
            "source": "manual",
            "mode": "live",
        },
    ]
    seed = build_seed_from_trades(trades, WINDOW_START, NOW, include_manual_trades=True)
    assert seed is not None
    assert seed["source"] == "manual"
    assert seed["amount"] > 0
    assert seed["average_entry"] == pytest.approx(0.27867)


def test_build_seed_excludes_manual_when_disabled():
    trades = [
        {
            "type": "BUY",
            "symbol": "H/USDT",
            "timestamp": "2026-06-13T15:16:07.635908",
            "price": 0.27867,
            "usdt_amount": 250.0,
            "amount": 0,
            "source": "manual",
            "mode": "live",
        },
    ]
    assert build_seed_from_trades(trades, WINDOW_START, NOW, include_manual_trades=False) is None


def test_pipeline_seeded_take_profit_delta(monkeypatch):
    monkeypatch.setattr(
        "hermes.cmc_replay.load_posts_for_coin",
        lambda *a, **k: [],
    )
    from hermes.counterfactual import _bar_index_for_ts

    df = _synthetic_ohlcv()
    buy_ms = int(datetime(2026, 6, 13, 15, 16, 7, tzinfo=timezone.utc).timestamp() * 1000)
    seed_bar = _bar_index_for_ts(df, buy_ms)
    assert seed_bar is not None
    base_params = {
        "rsi_sell_30": 70,
        "rsi_sell_20": 85,
        "take_profit_pct": 30,
        "stop_loss_pct": 50,
        "cmc_trust_score": 65,
        "cmc_min_confidence": 55,
        "buy_regime": "both",
    }
    var_params = dict(base_params, take_profit_pct=10)

    start_ms = int(WINDOW_START.timestamp() * 1000)
    end_ms = int(NOW.timestamp() * 1000)
    pipeline = PipelineBacktester()

    base = pipeline.run(
        "H/USDT",
        "4h",
        base_params,
        df,
        seed_bar=seed_bar,
        initial_position={"amount": 500.0, "average_entry": 0.28},
        window_start_ms=start_ms,
        window_end_ms=end_ms,
        window_metrics_only=True,
        allow_buys=False,
    )
    var = pipeline.run(
        "H/USDT",
        "4h",
        var_params,
        df,
        seed_bar=seed_bar,
        initial_position={"amount": 500.0, "average_entry": 0.28},
        window_start_ms=start_ms,
        window_end_ms=end_ms,
        window_metrics_only=True,
        allow_buys=False,
    )
    assert var.window_sells >= 1
    assert var.window_sell_pnl >= base.window_sell_pnl


def test_compare_params_window_with_mocked_ohlcv(monkeypatch):
    df = _synthetic_ohlcv()
    trades = [
        {
            "type": "BUY",
            "symbol": "H/USDT",
            "timestamp": "2026-06-13T15:16:07.635908",
            "price": 0.28,
            "usdt_amount": 140.0,
            "amount": 500.0,
            "source": "manual",
            "mode": "live",
        },
    ]
    monkeypatch.setattr(
        "hermes.counterfactual.Backtester._fetch_ohlcv",
        lambda *a, **k: df,
    )
    monkeypatch.setattr(
        "hermes.cmc_replay.load_posts_for_coin",
        lambda *a, **k: [],
    )

    result = compare_params_window(
        "H/USDT",
        "4h",
        {"take_profit_pct": 30, "rsi_sell_30": 70, "rsi_sell_20": 85, "stop_loss_pct": 50,
         "cmc_trust_score": 65, "cmc_min_confidence": 55, "buy_regime": "both"},
        {"take_profit_pct": 10, "rsi_sell_30": 70, "rsi_sell_20": 85, "stop_loss_pct": 50,
         "cmc_trust_score": 65, "cmc_min_confidence": 55, "buy_regime": "both"},
        WINDOW_START,
        NOW,
        trades=trades,
    )
    assert isinstance(result, CounterfactualResult)
    assert result.seeded is True
    assert result.seed_source == "manual"
    assert result.variant_sells >= 1