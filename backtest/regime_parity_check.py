#!/usr/bin/env python3
"""
Regime parity / smoke harness.
Runs the Python RegimeDetector + StrategyAllocator on synthetic data
and emits JSON report in similar shape to the Rust skeleton.
Can be used to validate Rust CLI output when available.
"""
import json
import sys
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intelligence.regime_detector import RegimeDetector
from intelligence.strategy_allocator import StrategyAllocator


def make_synth(n=800, trend=0.01):
    prices = [100.0]
    for i in range(1, n):
        p = prices[-1] + trend + (0.6 * ((i % 11) - 5) / 10.0) + 0.2 * (i % 7 - 3)
        prices.append(max(1.0, p))
    df = pd.DataFrame({
        "close": prices,
        "high": [p * 1.003 for p in prices],
        "low": [p * 0.997 for p in prices],
    })
    return df


def run_py_backtest(symbols):
    det = RegimeDetector({"tech_weight": 0.62, "sentiment_weight": 0.38, "cooldown_bars": 3})
    alloc = StrategyAllocator()
    total_equity = 10000.0
    total_trades = 0
    regime_stats = {}

    for sym in symbols:
        df = make_synth(800, 0.012 if "BTC" in sym else 0.007)
        equity = 1000.0
        for i in range(40, len(df)):
            window = df.iloc[max(0, i-60):i+1]
            price = float(df.iloc[i]["close"])
            sent = 0.6 * ((i / 80.0) % 2 - 1)   # oscillating fake
            res = det.detect(
                {"symbol": sym, "timeframe": "1h"},
                window,
                current_price=price,
                atr_pct=3.0,
                social_context={"lunarcrush_sentiment": 50 + sent * 40},
            )
            al = alloc.allocate(res, {"symbol": sym})
            # mock trade pnl contrib similar to rust
            ret = (price - float(df.iloc[i-5]["close"])) / max(1.0, float(df.iloc[i-5]["close"]))
            w_grid = al.strategy_weights.get("grid", 0.3)
            contrib = 0.0
            traded = False
            if "UPTREND" in res.primary_regime and al.strategy_weights.get("momentum", 0) > 0.5:
                contrib = ret * 0.7
                traded = True
            elif "RANGING" in res.primary_regime and w_grid > 0.5:
                contrib = -ret * 0.004 if abs(ret) > 0.01 else 0.002
                traded = True
            elif "DOWNTREND" in res.primary_regime:
                contrib = ret * -0.25
                traded = True
            if res.sentiment_score < -0.55:
                contrib *= 0.3
            equity += contrib * (equity * 0.2)
            if traded:
                total_trades += 1

            st = regime_stats.setdefault(res.primary_regime, {"bars": 0, "pnl": 0.0})
            st["bars"] += 1
            st["pnl"] += contrib

        total_equity += equity

    regimes = []
    for k, v in regime_stats.items():
        regimes.append({
            "regime": k,
            "bars": v["bars"],
            "pnl_contrib": round(v["pnl"], 4),
            "share": round(v["bars"] / 800.0, 3),
        })

    report = {
        "ok": True,
        "symbols": symbols,
        "metrics": {
            "final_equity": round(total_equity, 2),
            "trades": total_trades,
            "regime_distribution": regimes,
        },
        "note": "Python reference using real RegimeDetector+Allocator (synthetic). Compare vs Rust binary output.",
    }
    return report


if __name__ == "__main__":
    syms = ["BTC/USDT", "ETH/USDT"]
    if len(sys.argv) > 1:
        syms = sys.argv[1].split(",")
    rep = run_py_backtest(syms)
    print(json.dumps(rep, indent=2))
    out = Path("/tmp/regime_py_reference.json")
    out.write_text(json.dumps(rep, indent=2))
    print(f"Wrote {out}")
