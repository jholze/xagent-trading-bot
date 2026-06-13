"""Counterfactual replay — what would Hermes configs have done in a time window?"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone

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


def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def replay_window(
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    variants: dict | None = None,
) -> dict:
    config = get_bot_config()
    hermes = config.hermes_config
    backtester = Backtester(config)

    span_days = max(7, int((end - start).total_seconds() / 86400) + 2)
    ohlcv_df = backtester._fetch_ohlcv(symbol, timeframe, span_days)
    if ohlcv_df is None or ohlcv_df.empty:
        return {"error": f"No OHLCV for {symbol}"}

    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    mask = (ohlcv_df["ts"] >= start_ms) & (ohlcv_df["ts"] <= end_ms)
    window_df = ohlcv_df.loc[mask].copy()
    if len(window_df) < 12:
        window_df = ohlcv_df.tail(max(12, len(ohlcv_df) // 3))

    base_params = deepcopy(DEFAULT_PARAMS)
    base_params.update(config.strategy_params(symbol, timeframe) or {})

    results = {}
    for name, overrides in (variants or VARIANTS).items():
        params = deepcopy(base_params)
        params.update(overrides)
        saved_mode = hermes.get("backtest_mode", "ta_only")
        hermes["backtest_mode"] = "pipeline"
        try:
            result = backtester.run(symbol, timeframe, params, ohlcv_df=window_df)
            buys = [t for t in result.trades if t.get("type") == "BUY"]
            sells = [t for t in result.trades if t.get("type") == "SELL"]
            results[name] = {
                "params": overrides,
                "buys": len(buys),
                "sells": len(sells),
                "sharpe": result.metrics.sharpe,
                "opportunity_score": result.metrics.opportunity_score,
                "trade_quality": result.metrics.trade_quality,
                "win_rate": result.metrics.win_rate,
                "trades": result.metrics.trades,
                "sources": [t.get("sources", []) for t in buys[:5]],
            }
        finally:
            hermes["backtest_mode"] = saved_mode

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "window": f"{start.isoformat()} → {end.isoformat()}",
        "bars": len(window_df),
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