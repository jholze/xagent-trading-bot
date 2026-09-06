"""Rolling walk-forward validation for Hermes backtests."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import pandas as pd

from core.models import SandboxMetrics
from hermes.backtester import BACKTESTER_MIN_BARS, Backtester
from logger import log

_TF_MINUTES = {
    "1m": 1,
    "3m": 3,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "2h": 120,
    "4h": 240,
    "6h": 360,
    "8h": 480,
    "12h": 720,
    "1d": 1440,
    "3d": 4320,
    "1w": 10080,
}
_TF_RE = re.compile(r"^(\d+)(m|h|d|w)$")


@dataclass
class WalkForwardResult:
    symbol: str
    timeframe: str
    params: dict
    fold_metrics: list[dict] = field(default_factory=list)
    aggregate: SandboxMetrics = field(default_factory=SandboxMetrics)
    folds_total: int = 0
    folds_won: int = 0
    folds_excluded: int = 0


@dataclass
class FoldGeometry:
    """Walk-forward window vs BACKTESTER_MIN_BARS (#308)."""

    ok: bool
    min_bars_per_fold: int
    fold_days: int
    timeframes: list[str]
    issues: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)

    @property
    def message(self) -> str:
        if self.ok:
            return "; ".join(self.details) or "fold geometry ok"
        return (
            "Hermes fold geometry invalid — cycles paused. " + " ".join(self.issues)
        )


def timeframe_bars_per_day(timeframe: str) -> float:
    """Bars per calendar day for a ccxt-style timeframe (4h → 6)."""
    tf = str(timeframe or "4h").strip().lower()
    minutes = _TF_MINUTES.get(tf)
    if minutes is None:
        m = _TF_RE.fullmatch(tf)
        if not m:
            return 6.0
        n, unit = int(m.group(1)), m.group(2)
        minutes = {"m": n, "h": n * 60, "d": n * 1440, "w": n * 10080}[unit]
    if minutes <= 0:
        return 6.0
    return 1440 / minutes


def expected_fold_bars(fold_days: int, timeframe: str) -> int:
    return int(fold_days * timeframe_bars_per_day(timeframe))


def min_fold_days_for_timeframe(timeframe: str, min_bars: int = BACKTESTER_MIN_BARS) -> int:
    bpd = timeframe_bars_per_day(timeframe)
    if bpd <= 0:
        return int(min_bars)
    return int(math.ceil(min_bars / bpd))


def inspect_fold_geometry(hermes_cfg: dict | None) -> FoldGeometry:
    """Check min_bars_per_fold and fold_days against BACKTESTER_MIN_BARS (#308)."""
    cfg = hermes_cfg or {}
    vcfg = cfg.get("validation") or {}
    try:
        min_bars = int(vcfg.get("min_bars_per_fold", BACKTESTER_MIN_BARS))
    except (TypeError, ValueError):
        min_bars = BACKTESTER_MIN_BARS
    try:
        fold_days = int(vcfg.get("fold_days", 7))
    except (TypeError, ValueError):
        fold_days = 7
    timeframes = list(cfg.get("timeframes") or ["4h"])
    if not timeframes:
        timeframes = ["4h"]

    issues: list[str] = []
    details: list[str] = []
    if min_bars < BACKTESTER_MIN_BARS:
        issues.append(
            f"min_bars_per_fold={min_bars} < BACKTESTER_MIN_BARS={BACKTESTER_MIN_BARS}."
        )
    for tf in timeframes:
        bars = expected_fold_bars(fold_days, tf)
        bpd = timeframe_bars_per_day(tf)
        need_days = min_fold_days_for_timeframe(tf, BACKTESTER_MIN_BARS)
        details.append(f"fold_days={fold_days} on {tf} → {bars} bars ({bpd:g}/day)")
        if bars < BACKTESTER_MIN_BARS:
            issues.append(
                f"fold_days={fold_days} on {tf} yields {bars} bars "
                f"({bpd:g} bars/day); need fold_days >= {need_days} "
                f"for {BACKTESTER_MIN_BARS} bars."
            )
    return FoldGeometry(
        ok=not issues,
        min_bars_per_fold=min_bars,
        fold_days=fold_days,
        timeframes=timeframes,
        issues=issues,
        details=details,
    )


def _validation_cfg(hermes: dict) -> dict:
    return hermes.get("validation", {})


def rolling_folds(
    df: pd.DataFrame,
    fold_days: int = 7,
    step_days: int = 3,
    min_bars: int = BACKTESTER_MIN_BARS,
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

    opps = [float(m.get("opportunity_score", 0)) for m in fold_metrics]
    tqs = [float(m.get("trade_quality", 0)) for m in fold_metrics]
    buys = sum(int(m.get("buy_signals", 0)) for m in fold_metrics)

    return SandboxMetrics(
        win_rate=round(sum(win_rates) / len(win_rates), 1) if win_rates else 0.0,
        sharpe=round(sum(sharpes) / len(sharpes), 2) if sharpes else 0.0,
        max_drawdown_pct=round(max(dds) if dds else 0.0, 1),
        trades=trades,
        realized_pnl=round(pnl, 2),
        equity=round(equities[-1], 2) if equities else 0.0,
        trade_quality=round(sum(tqs) / len(tqs), 4) if tqs else 0.0,
        opportunity_score=round(sum(opps) / len(opps), 4) if opps else 0.0,
        buy_signals=buys,
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
    min_bars = int(vcfg.get("min_bars_per_fold", BACKTESTER_MIN_BARS))
    # Missing key → 1 so 0-trade folds are excluded but 1-trade folds still score (#308).
    min_trades_per_fold = int(vcfg.get("min_trades_per_fold", 1))

    folds = rolling_folds(ohlcv_df, fold_days, step_days, min_bars)
    if not folds:
        log(f"Hermes walk-forward: no valid folds for {symbol} {timeframe}", "WARNING")
        return WalkForwardResult(symbol=symbol, timeframe=timeframe, params=params)

    fold_metrics = []
    folds_won = 0
    folds_excluded = 0
    for fold_id, slice_df in folds:
        result = backtester.run(symbol, timeframe, params, ohlcv_df=slice_df)
        metrics = result.metrics.__dict__
        metrics["fold_id"] = fold_id
        metrics["bars"] = len(slice_df)
        metrics["excluded"] = False

        if baseline_folds is not None:
            base = next((f for f in baseline_folds if f.get("fold_id") == fold_id), None)
            if base is not None:
                b_tr = int(base.get("trades") or 0)
                v_tr = int(metrics.get("trades") or 0)
                if b_tr < min_trades_per_fold or v_tr < min_trades_per_fold:
                    metrics["excluded"] = True
                    metrics["exclude_reason"] = (
                        f"trades base={b_tr} var={v_tr} "
                        f"< min_trades_per_fold={min_trades_per_fold}"
                    )
                    folds_excluded += 1
                elif metrics.get("sharpe", 0) > base.get("sharpe", 0):
                    folds_won += 1
        fold_metrics.append(metrics)

    if folds_excluded:
        log(
            f"Hermes walk-forward {symbol} {timeframe}: "
            f"{folds_excluded}/{len(fold_metrics)} folds excluded "
            f"(min_trades_per_fold={min_trades_per_fold})",
            "INFO",
        )

    return WalkForwardResult(
        symbol=symbol,
        timeframe=timeframe,
        params=params,
        fold_metrics=fold_metrics,
        aggregate=_aggregate_metrics(fold_metrics),
        folds_total=len(fold_metrics),
        folds_won=folds_won,
        folds_excluded=folds_excluded,
    )