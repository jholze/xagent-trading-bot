"""Counterfactual replay — what would Hermes configs have done in a time window?"""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd

from core.config import get_bot_config
from hermes.backtester import Backtester
from hermes.memory.store import DEFAULT_PARAMS
from logger import log


VARIANTS = {
    "baseline_dip": {
        "buy_regime": "dip",
        "cmc_trust_score": 65.0,
        "cmc_min_confidence": 60.0,
    },
    "reversal_both": {
        "buy_regime": "both",
        "reversal_rsi_cross_low": 32,
        "reversal_rsi_cross_high": 38,
        "reversal_volume_multiplier": 1.2,
        "cmc_trust_score": 65.0,
        "cmc_min_confidence": 60.0,
    },
    "fusion_tuned": {
        "buy_regime": "both",
        "cmc_trust_score": 75.0,
        "cmc_min_confidence": 55.0,
        "reversal_rsi_cross_low": 30,
        "reversal_rsi_cross_high": 36,
        "reversal_volume_multiplier": 1.2,
    },
    "enhanced_dry_run": {
        "buy_regime": "both",
        "cmc_trust_score": 65.0,
        "cmc_min_confidence": 55.0,
    },
}


@dataclass
class CounterfactualResult:
    baseline_pnl: float
    variant_pnl: float
    pnl_delta: float
    baseline_sells: int
    variant_sells: int
    seeded: bool
    seed_source: str | None
    window_start: datetime
    window_end: datetime

    def to_dict(self) -> dict:
        return {
            "baseline_pnl": self.baseline_pnl,
            "variant_pnl": self.variant_pnl,
            "pnl_delta": self.pnl_delta,
            "baseline_sells": self.baseline_sells,
            "variant_sells": self.variant_sells,
            "seeded": self.seeded,
            "seed_source": self.seed_source,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
        }


def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _bar_index_for_ts(ohlcv_df: pd.DataFrame, ts_ms: int) -> int | None:
    if ohlcv_df is None or ohlcv_df.empty:
        return None
    from hermes.pipeline_backtest import PipelineBacktester

    processed = PipelineBacktester()._add_indicators(ohlcv_df.copy())
    if processed.empty or len(processed) < 21:
        return None
    idx = int(processed["ts"].searchsorted(ts_ms, side="left"))
    idx = min(idx, len(processed) - 1)
    return max(20, idx)


def build_seed_from_trades(
    trades: list[dict],
    window_start: datetime,
    window_end: datetime,
    include_manual_trades: bool = True,
) -> dict | None:
    """Find first BUY in window to seed counterfactual position."""
    start_ms = int(window_start.timestamp() * 1000)
    end_ms = int(window_end.timestamp() * 1000)
    candidates = []
    for trade in sorted(trades, key=lambda t: t.get("timestamp", "")):
        if trade.get("type") != "BUY":
            continue
        source = trade.get("source") or "unknown"
        if not include_manual_trades and source == "manual":
            continue
        ts = _parse_dt(str(trade["timestamp"]))
        ts_ms = int(ts.timestamp() * 1000)
        if ts_ms < start_ms or ts_ms > end_ms:
            continue
        price = float(trade.get("price") or 0)
        if price <= 0:
            continue
        usdt = float(trade.get("usdt_amount") or trade.get("usdt_received") or 0)
        amount = float(trade.get("amount") or 0)
        if amount <= 0 and usdt > 0:
            amount = usdt / price
        if amount <= 0:
            continue
        candidates.append({
            "ts_ms": ts_ms,
            "amount": amount,
            "average_entry": price,
            "source": source,
        })
    return candidates[0] if candidates else None


def _run_params_in_window(
    backtester: Backtester,
    symbol: str,
    timeframe: str,
    params: dict,
    ohlcv_df: pd.DataFrame,
    seed: dict,
    window_start: datetime,
    window_end: datetime,
) -> tuple[float, int]:
    from hermes.pipeline_backtest import PipelineBacktester

    seed_bar = _bar_index_for_ts(ohlcv_df, seed["ts_ms"])
    if seed_bar is None:
        return 0.0, 0

    start_ms = int(window_start.timestamp() * 1000)
    end_ms = int(window_end.timestamp() * 1000)
    pipeline = PipelineBacktester(backtester.config)
    result = pipeline.run(
        symbol,
        timeframe,
        params,
        ohlcv_df,
        seed_bar=seed_bar,
        initial_position={
            "amount": seed["amount"],
            "average_entry": seed["average_entry"],
        },
        initial_sim_state={"last_rsi": 45.0, "rsi_sell_tiers_done": {}},
        window_start_ms=start_ms,
        window_end_ms=end_ms,
        window_metrics_only=True,
        allow_buys=False,
    )
    return result.window_sell_pnl, result.window_sells


def compare_params_window(
    symbol: str,
    timeframe: str,
    baseline_params: dict,
    variant_params: dict,
    start: datetime,
    end: datetime,
    *,
    include_manual_trades: bool = True,
    trades: list[dict] | None = None,
) -> CounterfactualResult | None:
    config = get_bot_config()
    backtester = Backtester(config)

    if trades is None:
        from data_manager import load_live_trade_history
        history = load_live_trade_history()
        trades = [
            t for t in history.get("trades", [])
            if t.get("symbol") == symbol and t.get("mode", "live") == "live"
        ]

    seed = build_seed_from_trades(trades, start, end, include_manual_trades)
    if not seed:
        return None

    span_days = max(7, int((end - start).total_seconds() / 86400) + 3)
    ohlcv_df = backtester._fetch_ohlcv(symbol, timeframe, span_days)
    if ohlcv_df is None or ohlcv_df.empty:
        log(f"Counterfactual: no OHLCV for {symbol}", "WARNING")
        return None

    base_pnl, base_sells = _run_params_in_window(
        backtester, symbol, timeframe, baseline_params, ohlcv_df, seed, start, end,
    )
    var_pnl, var_sells = _run_params_in_window(
        backtester, symbol, timeframe, variant_params, ohlcv_df, seed, start, end,
    )

    return CounterfactualResult(
        baseline_pnl=round(base_pnl, 4),
        variant_pnl=round(var_pnl, 4),
        pnl_delta=round(var_pnl - base_pnl, 4),
        baseline_sells=base_sells,
        variant_sells=var_sells,
        seeded=True,
        seed_source=seed.get("source"),
        window_start=start,
        window_end=end,
    )


def replay_window(
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    variants: dict | None = None,
) -> dict:
    config = get_bot_config()
    backtester = Backtester(config)

    span_days = max(7, int((end - start).total_seconds() / 86400) + 2)
    ohlcv_df = backtester._fetch_ohlcv(symbol, timeframe, span_days)
    if ohlcv_df is None or ohlcv_df.empty:
        return {"error": f"No OHLCV for {symbol}"}

    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    window_mask = (ohlcv_df["ts"] >= start_ms) & (ohlcv_df["ts"] <= end_ms)
    window_bars = int(window_mask.sum())

    base_params = deepcopy(DEFAULT_PARAMS)
    base_params.update(config.strategy_params(symbol, timeframe) or {})

    results = {}
    for name, overrides in (variants or VARIANTS).items():
        params = deepcopy(base_params)
        params.update(overrides)
        saved_mode = config.hermes_config.get("backtest_mode", "ta_only")
        config.hermes_config["backtest_mode"] = "pipeline"
        try:
            result = backtester.run(symbol, timeframe, params, ohlcv_df=ohlcv_df)

            def _in_window(trade: dict) -> bool:
                bar_idx = trade.get("bar")
                if bar_idx is None:
                    return False
                ts = int(ohlcv_df.iloc[bar_idx]["ts"])
                return start_ms <= ts <= end_ms

            buys = [t for t in result.trades if t.get("type") == "BUY" and _in_window(t)]
            sells = [t for t in result.trades if t.get("type") == "SELL" and _in_window(t)]
            results[name] = {
                "params": overrides,
                "buys": len(buys),
                "sells": len(sells),
                "sharpe": result.metrics.sharpe,
                "opportunity_score": result.metrics.opportunity_score,
                "trade_quality": result.metrics.trade_quality,
                "win_rate": result.metrics.win_rate,
                "trades": len(sells),
                "sources": [t.get("sources", []) for t in buys[:5]],
            }
        finally:
            config.hermes_config["backtest_mode"] = saved_mode

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "window": f"{start.isoformat()} → {end.isoformat()}",
        "bars": window_bars,
        "variants": results,
    }


def format_report(report: dict) -> str:
    if report.get("error"):
        return f"Error: {report['error']}"
    lines = [
        f"Counterfactual: {report['symbol']} {report['timeframe']}",
        f"Window: {report['window']} ({report['bars']} bars)",
        "",
    ]
    for name, data in report.get("variants", {}).items():
        lines.append(
            f"  {name}: buys={data['buys']} sells={data['sells']} "
            f"sharpe={data['sharpe']} opp={data['opportunity_score']} "
            f"tq={data['trade_quality']}"
        )
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Hermes counterfactual replay")
    parser.add_argument("--symbol", default="ARIA/USDT")
    parser.add_argument("--timeframe", default="4h")
    parser.add_argument("--from", dest="from_dt", default="2026-06-12T23:00:00")
    parser.add_argument("--to", dest="to_dt", default="2026-06-13T12:00:00")
    args = parser.parse_args()

    report = replay_window(
        args.symbol,
        args.timeframe,
        _parse_dt(args.from_dt),
        _parse_dt(args.to_dt),
    )
    print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())