#!/usr/bin/env python3
"""Preview new exit rules on open positions (read-only)."""

from __future__ import annotations

import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("DEMO_MODE", "1")

import ccxt

from core.models import MarketContext
from strategies.positions import load_positions, is_open_position, positions, get_key
from strategies.profit_max_lifetime import evaluate_profit_max_lifetime, sync_profit_armed_at
from strategies.trailing_take_profit import evaluate_trailing_take_profit
from strategies.registry import resolve_strategy_params


def main():
    scope = os.environ.get("LEDGER_SCOPE", "demo")
    load_positions(scope)
    ex = ccxt.gate({"enableRateLimit": True})
    now = datetime.now()
    rows = []

    for key, pos in sorted(positions.items()):
        if not is_open_position(pos):
            continue
        base, _, tf = key.rpartition("_")
        symbol = base.replace("_", "/")
        try:
            mark = float(ex.fetch_ticker(symbol)["last"])
        except Exception:
            continue
        entry = float(pos.get("average_entry") or 0)
        if entry <= 0:
            continue
        gain = (mark / entry - 1) * 100
        recent_high = float(pos.get("recent_high") or 0) or mark
        peak = (recent_high / entry - 1) * 100

        coin = {"symbol": symbol, "timeframe": tf}
        params = resolve_strategy_params(
            coin, has_position=True, frozen_tier=pos.get("strategy_tier"),
        )
        market = MarketContext(
            symbol=symbol,
            timeframe=tf,
            current_price=mark,
            has_position=True,
            average_entry=entry,
            atr_pct=5.0,
            strategy_params=params,
        )
        sync_profit_armed_at(market, pos, params, now=now)
        ttp = evaluate_trailing_take_profit(market, pos, params, now=now)
        life = evaluate_profit_max_lifetime(market, pos, params, now=now)

        rows.append({
            "symbol": symbol,
            "gain": gain,
            "peak": peak,
            "trail": ttp.action if ttp else "-",
            "life": life.action if life else "-",
            "steps": int(pos.get("trail_tp_steps", 0) or 0),
            "armed": bool(pos.get("profit_armed_at")),
        })

    if not rows:
        print("No open positions.")
        return

    print(f"{'Symbol':14} {'Gain':>6} {'Peak':>6} {'TrailTP':>12} {'Life':>12} steps armed")
    print("-" * 62)
    for r in rows:
        print(
            f"{r['symbol']:14} {r['gain']:5.1f}% {r['peak']:5.1f}% "
            f"{r['trail']:>12} {r['life']:>12} {r['steps']:>5} {str(r['armed']):>5}"
        )
    would_sell = sum(1 for r in rows if r["trail"] != "-" or r["life"] != "-")
    print(f"\n{len(rows)} open — {would_sell} would trigger exit signal now")


if __name__ == "__main__":
    main()