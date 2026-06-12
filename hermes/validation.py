"""Rolling walk-forward validation for Hermes backtests."""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from core.models import SandboxMetrics
from hermes.backtester import Backtester
from logger import log


@dataclass
class WalkForwardResult:
    symbol: str
    timeframe: str
    params: dict
    fold_metrics: list[dict] = field(default_factory=list)
    aggregate: SandboxMetrics = field(default_factory=SandboxMetrics)
    folds_total: int = 0
    folds_won: int = 0


def _validation_cfg(hermes: dict) -> dict:
    return hermes.get("validation", {})


def rolling_folds(
    df: pd.DataFrame,
    fold_days: int = 7,
    step_days: int = 3,
    min_bars: int = 12,
) -> list[tuple[int, pd.DataFrame]]:
    if df is None or df.empty:
        return []

    ts = df["ts"].astype("int64")
    start_ms = int(ts.iloc[0])
    end_ms = int(ts.iloc[-1])
    fold_ms = fold_days * 86_400_000
    step_ms = step_days * 86_400_000

    folds = []
    window_start = start_ms
    fold_id = 0
    while window_start + fold_ms <= end_ms:
        window_end = window_start + fold_ms
        mask = (ts >= window_start) & (ts < window_end)
        slice_df = df.loc[mask].copy()
        if len(slice_df) >= min_bars:
            folds.append((fold_id, slice_df))
            fold_id += 1
        window_start += step_ms
    return folds


def _aggregate_metrics(fold_metrics: list[dict]) -> SandboxMetrics:
    if not fold_metrics:
        return SandboxMetrics()

    sharpes = [float(m.get("sharpe", 0)) for m in fold_metrics]
    dds = [float(m.get("max_drawdown_pct", 0)) for m in fold_metrics]
    win_rates = [float(m.get("win_rate", 0)) for m in fold_metrics]
    trades = sum(int(m.get("trades", 0)) for m in fold_metrics)
    pnl = sum(float(m.get("realized_pnl", 0)) for m in fold_metrics)
    equities = [float(m.get("equity", 0)) for m in fold_metrics if m.get("equity")]

    return SandboxMetrics(
        win_rate=round(sum(win_rates) / len(win_rates), 1) if win_rates else 0.0,
        sharpe=round(sum(sharpes) / len(sharpes), 2) if sharpes else 0.0,
        max_drawdown_pct=round(max(dds) if dds else 0.0, 1),
        trades=trades,
        realized_pnl=round(pnl, 2),
        equity=round(equities[-1], 2) if equities else 0.0,
    )


def run_walk_forward(
    backtester: Backtester,
    symbol: str,
    timeframe: str,
    params: dict,
    ohlcv_df: pd.DataFrame,
    hermes_cfg: dict,
    baseline_folds: list[dict] | None = None,
) -> WalkForwardResult:
    vcfg = _validation_cfg(hermes_cfg)
    fold_days = int(vcfg.get("fold_days", 7))
    step_days = int(vcfg.get("step_days", 3))
    min_bars = int(vcfg.get("min_bars_per_fold", 12))

    folds = rolling_folds(ohlcv_df, fold_days, step_days, min_bars)
    if not folds:
        log(f"Hermes walk-forward: no valid folds for {symbol} {timeframe}", "WARNING")
        return WalkForwardResult(symbol=symbol, timeframe=timeframe, params=params)

    fold_metrics = []
    folds_won = 0
    for fold_id, slice_df in folds:
        result = backtester.run(symbol, timeframe, params, ohlcv_df=slice_df)
        metrics = result.metrics.__dict__
        metrics["fold_id"] = fold_id
        metrics["bars"] = len(slice_df)
        fold_metrics.append(metrics)

        if baseline_folds is not None:
            base = next((f for f in baseline_folds if f.get("fold_id") == fold_id), None)
            if base and metrics.get("sharpe", 0) > base.get("sharpe", 0):
                folds_won += 1

    return WalkForwardResult(
        symbol=symbol,
        timeframe=timeframe,
        params=params,
        fold_metrics=fold_metrics,
        aggregate=_aggregate_metrics(fold_metrics),
        folds_total=len(fold_metrics),
        folds_won=folds_won,
    )