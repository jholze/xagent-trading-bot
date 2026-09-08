import numpy as np
import pandas as pd
import pytest
from unittest.mock import patch

from hermes.backtester import Backtester
from hermes.goals import GoalEngine
from tests.support.offline import gate_prices_listed


def _synthetic_ohlcv(rows: int = 120) -> pd.DataFrame:
    """Generate OHLCV with occasional dips suitable for RSI/BB buy signals."""
    np.random.seed(42)
    close = 100 + np.cumsum(np.random.randn(rows) * 0.5)
    for i in range(30, rows, 25):
        close[i:i + 3] -= 8
    high = close + np.abs(np.random.randn(rows))
    low = close - np.abs(np.random.randn(rows))
    volume = np.random.uniform(1000, 5000, rows)
    volume[30::25] *= 3
    return pd.DataFrame({
        "ts": range(rows),
        "open": close,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    })


@patch("price_fetcher.get_gate_prices_batch", side_effect=gate_prices_listed)
@patch("price_fetcher.get_ticker_price", return_value=1.0)
def test_backtester_runs_on_synthetic_data(_ticker, _gate):
    bt = Backtester()
    # Isolate from production hermes.backtest_mode=pipeline (full DE + ccxt).
    bt.hermes = {**bt.hermes, "backtest_mode": "ta_only"}
    params = {
        "rsi_buy_low": 20,
        "rsi_buy_high": 55,
        "volume_multiplier": 1.0,
        "rsi_sell_30": 70,
        "rsi_sell_20": 85,
        "stop_loss_pct": 12.0,
    }
    df = _synthetic_ohlcv()
    result = bt.run("TEST/USDT", "4h", params, ohlcv_df=df)
    assert result.bars_tested > 0
    assert result.metrics.trades >= 0
    assert isinstance(result.metrics.sharpe, float)


def test_goal_engine_promotes_better_sharpe():
    goals = GoalEngine()
    baseline = {"sharpe": 0.5, "max_drawdown_pct": 10, "win_rate": 55, "trades": 6}
    variant = {"sharpe": 0.9, "max_drawdown_pct": 12, "win_rate": 58, "trades": 7}
    verdict = goals.evaluate(baseline, variant)
    assert verdict.promoted is True


def test_goal_engine_rejects_improvement_below_success_criteria():
    goals = GoalEngine()
    baseline = {"sharpe": 0.4, "max_drawdown_pct": 10, "win_rate": 40, "trades": 2}
    variant = {"sharpe": 0.5, "max_drawdown_pct": 10, "win_rate": 40, "trades": 3}
    verdict = goals.evaluate(baseline, variant)
    assert verdict.promoted is False


def test_backtest_uses_sim_state_not_global_positions(monkeypatch):
    import strategies.positions as positions_mod

    monkeypatch.setattr("price_fetcher.get_gate_prices_batch", gate_prices_listed)
    monkeypatch.setattr("price_fetcher.get_ticker_price", lambda symbol, exchange=None: 1.0)

    called = {"save": False}

    def fake_save():
        called["save"] = True

    monkeypatch.setattr(positions_mod, "save_positions", fake_save)
    bt = Backtester()
    bt.hermes = {**bt.hermes, "backtest_mode": "ta_only"}
    params = {
        "rsi_buy_low": 20,
        "rsi_buy_high": 55,
        "volume_multiplier": 1.0,
        "rsi_sell_30": 70,
        "rsi_sell_20": 85,
        "stop_loss_pct": 12.0,
    }
    result = bt.run("TEST/USDT", "4h", params, ohlcv_df=_synthetic_ohlcv())
    assert called["save"] is False
    assert result.bars_tested > 0


def test_goal_engine_rejects_worse_sharpe():
    goals = GoalEngine()
    baseline = {"sharpe": 0.8, "max_drawdown_pct": 10, "win_rate": 55, "trades": 6}
    variant = {"sharpe": 0.5, "max_drawdown_pct": 10, "win_rate": 50, "trades": 5}
    verdict = goals.evaluate(baseline, variant)
    assert verdict.promoted is False