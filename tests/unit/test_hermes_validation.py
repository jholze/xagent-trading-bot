import numpy as np
import pandas as pd

from core.models import SandboxMetrics
from hermes.backtester import Backtester
from hermes.goals import GoalEngine
from hermes.validation import WalkForwardResult, rolling_folds, run_walk_forward


def _synthetic_ohlcv(days: int = 35) -> pd.DataFrame:
    bars_per_day = 6
    n = days * bars_per_day
    start_ms = 1_700_000_000_000
    step_ms = 4 * 3600 * 1000
    ts = [start_ms + i * step_ms for i in range(n)]
    close = 100 + np.cumsum(np.random.default_rng(1).normal(0, 0.5, n))
    return pd.DataFrame({
        "ts": ts,
        "open": close,
        "high": close + 1,
        "low": close - 1,
        "close": close,
        "volume": np.random.default_rng(2).uniform(1000, 5000, n),
    })


def test_rolling_folds_produces_multiple_windows():
    df = _synthetic_ohlcv(35)
    folds = rolling_folds(df, fold_days=7, step_days=3, min_bars=12)
    assert len(folds) >= 8


def test_walk_forward_folds_won():
    bt = Backtester()
    hermes = {
        "validation": {
            "fold_days": 7,
            "step_days": 3,
            "min_bars_per_fold": 12,
        }
    }
    params = {
        "rsi_buy_low": 20,
        "rsi_buy_high": 55,
        "volume_multiplier": 1.0,
        "rsi_sell_30": 70,
        "rsi_sell_20": 85,
        "stop_loss_pct": 12.0,
    }
    df = _synthetic_ohlcv(35)
    base = run_walk_forward(bt, "TEST/USDT", "4h", params, df, hermes)
    better = dict(params)
    better["rsi_buy_low"] = 15
    var = run_walk_forward(bt, "TEST/USDT", "4h", better, df, hermes, baseline_folds=base.fold_metrics)
    assert var.folds_total == base.folds_total
    assert 0 <= var.folds_won <= var.folds_total


def test_goal_engine_rejects_low_fold_win_ratio():
    goals = GoalEngine()
    base = WalkForwardResult(
        symbol="T", timeframe="4h", params={},
        fold_metrics=[{"fold_id": i, "sharpe": 0.5, "max_drawdown_pct": 5, "trades": 2} for i in range(10)],
        aggregate=SandboxMetrics(sharpe=0.5, max_drawdown_pct=5, win_rate=55, trades=20),
        folds_total=10, folds_won=0,
    )
    var = WalkForwardResult(
        symbol="T", timeframe="4h", params={},
        fold_metrics=[{"fold_id": i, "sharpe": 0.9, "max_drawdown_pct": 5, "trades": 2} for i in range(10)],
        aggregate=SandboxMetrics(sharpe=0.9, max_drawdown_pct=5, win_rate=60, trades=20),
        folds_total=10, folds_won=4,
    )
    verdict = goals.evaluate_walk_forward(base, var)
    assert verdict.promoted is False