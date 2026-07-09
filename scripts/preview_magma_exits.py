#!/usr/bin/env python3
"""Read-only MAGMA exit scenario player against test ledger."""

from __future__ import annotations

import argparse
import copy
import os
import sys
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("DEMO_MODE", "1")
os.environ.setdefault("DEMO_LEDGER_BACKEND", "mongo")
os.environ.setdefault("MONGODB_DB", "xagent_test")

import ccxt

from core.actions import normalize
from core.config import get_bot_config
from core.models import MarketContext
from strategies.decision_engine import DecisionEngine
from strategies.exit_ladder import current_ladder_step, ladder_config
from strategies.positions import bootstrap_positions, get_key, list_active_positions, positions
from strategies.profit_max_lifetime import evaluate_profit_max_lifetime, sync_profit_armed_at
from strategies.registry import get_strategy, resolve_strategy_params
from strategies.trailing_take_profit import evaluate_trailing_take_profit
from strategies.entry_guard import is_fresh_guarded_entry, entry_guard_config
from storage.mongo_client import resolve_database_name, resolve_mongo_uri


def _pct(entry: float, price: float) -> float:
    if entry <= 0:
        return 0.0
    return (price / entry - 1) * 100


def _build_market(
    symbol: str,
    tf: str,
    price: float,
    pos: dict,
    params: dict,
    *,
    rsi: float = 50.0,
    sim_state: dict | None = None,
) -> MarketContext:
    entry = float(pos.get("average_entry") or 0)
    return MarketContext(
        symbol=symbol,
        timeframe=tf,
        current_price=price,
        rsi=rsi,
        lower_bb=price * 0.95,
        upper_bb=price * 1.05,
        middle_bb=price,
        atr_pct=8.0,
        vol_multiplier=1.2,
        has_position=True,
        average_entry=entry,
        open_positions=1,
        strategy_params=params,
        sim_state=sim_state,
    )


def _eval_rules(
    market: MarketContext,
    pos: dict,
    params: dict,
    *,
    now: datetime,
    coin: dict,
) -> dict:
    pos_copy = copy.deepcopy(pos)
    sim = copy.deepcopy(pos_copy)
    if market.sim_state:
        sim.update(market.sim_state)

    sync_profit_armed_at(market, pos_copy, params, now=now)
    trail = evaluate_trailing_take_profit(market, pos_copy, params, now=now)
    life = evaluate_profit_max_lifetime(market, pos_copy, params, now=now)

    tech_market = MarketContext(
        symbol=market.symbol,
        timeframe=market.timeframe,
        current_price=market.current_price,
        rsi=market.rsi,
        lower_bb=market.lower_bb,
        upper_bb=market.upper_bb,
        middle_bb=market.middle_bb,
        atr_pct=market.atr_pct,
        vol_multiplier=market.vol_multiplier,
        has_position=True,
        average_entry=market.average_entry,
        open_positions=market.open_positions,
        strategy_params=params,
        sim_state=sim,
    )
    strategy = get_strategy({**coin, "strategy_params": params})
    technical = strategy.analyze(coin, tech_market)

    return {
        "technical": technical.action,
        "trail": trail.action if trail else "-",
        "life": life.action if life else "-",
        "armed": bool(pos_copy.get("profit_armed_at")),
        "sources": list(technical.sources),
    }


def _eval_engine(
    coin: dict,
    market: MarketContext,
    pos: dict,
    *,
    now: datetime,
) -> str:
    key = get_key(coin["symbol"], market.timeframe)
    saved = positions.get(key)
    positions[key] = copy.deepcopy(pos)
    try:
        engine = DecisionEngine()
        result = engine.evaluate_with_market(coin, market)
        return normalize(result.normalized_action)
    finally:
        if saved is not None:
            positions[key] = saved
        elif key in positions:
            del positions[key]


def _scenario_rows(entry: float, params: dict) -> list[dict]:
    ttp = params.get("trailing_take_profit") or {}
    life_cfg = params.get("profit_max_lifetime") or {}
    arm_peak = float(ttp.get("arm_gain_pct", 12))
    min_gain = float(ttp.get("min_gain_pct", 10))
    trail_pct = float(ttp.get("trail_pct", 6))
    rsi_min = float(params.get("rsi_sell_min_gain_pct", 10))
    rsi_30 = float(params.get("rsi_sell_30", 62))

    peak25_price = entry * 1.25
    # Slightly over trail_pct to avoid float edge (5.999% < 6%)
    peak25_drop = peak25_price * (1 - (trail_pct + 0.5) / 100)
    peak15_price = entry * 1.15
    peak15_pull8 = entry * 1.08

    return [
        {
            "name": "live (mark price)",
            "price": None,
            "rsi": None,
            "recent_high": None,
            "profit_armed_hours_ago": None,
        },
        {
            "name": f"RSI ladder +{rsi_min + 1:.0f}% RSI {rsi_30:.0f}",
            "price": entry * (1 + (rsi_min + 1) / 100),
            "rsi": rsi_30 + 0.5,
            "recent_high": None,
            "profit_armed_hours_ago": None,
        },
        {
            "name": f"TrailTP peak +25% drop {trail_pct + 0.5:.1f}%",
            "price": peak25_drop,
            "rsi": 55.0,
            "recent_high": peak25_price,
            "profit_armed_hours_ago": None,
        },
        {
            "name": f"TrailTP peak +15% pullback +8% (no min_gain)",
            "price": peak15_pull8,
            "rsi": 55.0,
            "recent_high": peak15_price,
            "profit_armed_hours_ago": None,
        },
        {
            "name": f"Profit-Lifetime +8% after {life_cfg.get('max_hours', 96)+4:.0f}h armed",
            "price": entry * 1.08,
            "rsi": 50.0,
            "recent_high": entry * 1.10,
            "profit_armed_hours_ago": float(life_cfg.get("max_hours", 96)) + 4,
        },
        {
            "name": f"Below TrailTP (peak +{arm_peak:.0f}% only)",
            "price": entry * (1 + (arm_peak - 1) / 100),
            "rsi": 50.0,
            "recent_high": entry * (1 + arm_peak / 100),
            "profit_armed_hours_ago": None,
        },
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="MAGMA exit scenario preview (read-only)")
    parser.add_argument("--symbol", default="MAGMA/USDT", help="Symbol to preview")
    parser.add_argument("--live-only", action="store_true", help="Only show live snapshot")
    args = parser.parse_args()

    uri = resolve_mongo_uri()
    db = resolve_database_name()
    if not uri:
        print("MONGO_URL/MONGODB_URI not set — cannot load test ledger")
        return 1
    print(f"Ledger: mongo/{db} (read-only)\n")

    scope = os.environ.get("LEDGER_SCOPE", "demo")
    bootstrap_positions(scope)

    active = list_active_positions()
    sym_base = args.symbol.split("/")[0]
    pos_row = next((p for p in active if p["symbol"].startswith(sym_base)), None)
    if not pos_row:
        print(f"No open position for {args.symbol}")
        print("Open:", ", ".join(p["symbol"] for p in active) or "(none)")
        return 1

    symbol = pos_row["symbol"]
    tf = pos_row.get("timeframe", "1h")
    key = get_key(symbol, tf)
    pos = copy.deepcopy(positions.get(key) or pos_row)

    ex = ccxt.gate({"enableRateLimit": True})
    try:
        live_price = float(ex.fetch_ticker(symbol)["last"])
    except Exception as exc:
        print(f"Price fetch failed: {exc}")
        return 1

    entry = float(pos.get("average_entry") or 0)
    amount = float(pos.get("amount") or 0)
    sold = float(pos.get("sold_percent") or 0)
    params = resolve_strategy_params(
        {"symbol": symbol, "timeframe": tf},
        has_position=True,
        frozen_tier=pos.get("strategy_tier"),
    )
    coin = {"symbol": symbol, "timeframe": tf, "strategy_params": params}
    now = datetime.now()

    recent_high = float(pos.get("recent_high") or 0)
    if recent_high <= 0:
        recent_high = live_price
        pos["recent_high"] = recent_high

    gain = _pct(entry, live_price)
    peak = _pct(entry, recent_high)
    notional = live_price * amount
    ladder = ladder_config(params)
    step = current_ladder_step(pos, ladder.get("tiers") or [])

    profile = params.get("strategy_profile", "?")
    tier = params.get("volatility_tier", pos.get("strategy_tier", "?"))
    print(f"=== {symbol} @ {tf} ({profile}, tier={tier}) ===")
    print(f"Entry: ${entry:.6f}  Mark: ${live_price:.6f}  Gain: {gain:+.1f}%")
    print(f"Peak (ledger): {peak:+.1f}%  Notional: ${notional:,.0f}  Sold: {sold*100:.0f}%")
    print(f"Exit ladder step {step}/{len(ladder.get('tiers') or [])}  tiers={ladder.get('tiers')}")
    guard_cfg = entry_guard_config()
    fresh = is_fresh_guarded_entry(pos, guard_cfg, as_of=now)
    if fresh:
        print(
            f"Entry-Guard: FRESH (blocks partial TA sells below "
            f"{guard_cfg.get('mega_pump_gain_pct', 12)}% gain)"
        )
    print()

    ttp_cfg = params.get("trailing_take_profit") or {}
    life_cfg = params.get("profit_max_lifetime") or {}
    print("Thresholds:")
    print(
        f"  TrailTP: arm_peak>={ttp_cfg.get('arm_gain_pct')}% "
        f"min_gain>={ttp_cfg.get('min_gain_pct')}% trail={ttp_cfg.get('trail_pct')}%"
    )
    print(
        f"  RSI: level mode sell_30>={params.get('rsi_sell_30')} "
        f"min_gain>={params.get('rsi_sell_min_gain_pct')}%"
    )
    print(
        f"  Life: arm>={life_cfg.get('arm_gain_pct')}% "
        f"max_hours={life_cfg.get('max_hours')} skip_peak>{life_cfg.get('skip_if_peak_above_pct')}%"
    )
    print()

    live_market = _build_market(symbol, tf, live_price, pos, params, rsi=50.0)
    live_rules = _eval_rules(live_market, pos, params, now=now, coin=coin)
    live_engine = _eval_engine(coin, live_market, pos, now=now)

    print("=== LIVE NOW ===")
    print(
        f"  technical={live_rules['technical']}  trail={live_rules['trail']}  "
        f"life={live_rules['life']}  engine={live_engine}"
    )
    if recent_high <= 0 or float(pos.get("recent_high") or 0) == 0:
        print("  note: recent_high was 0 in ledger — using live mark as peak proxy")
    print()

    if args.live_only:
        return 0

    print("=== SCENARIO MATRIX ===")
    print(f"{'Scenario':<42} {'Gain':>6} {'Peak':>6} {'Tech':>10} {'Trail':>12} {'Life':>12} {'Engine':>10}")
    print("-" * 102)

    for row in _scenario_rows(entry, params):
        price = live_price if row["price"] is None else float(row["price"])
        rsi = 50.0 if row["rsi"] is None else float(row["rsi"])
        sim_pos = copy.deepcopy(pos)
        if row["recent_high"] is not None:
            sim_pos["recent_high"] = float(row["recent_high"])
        elif price > float(sim_pos.get("recent_high") or 0):
            sim_pos["recent_high"] = price

        if row["profit_armed_hours_ago"] is not None:
            armed_at = now - timedelta(hours=float(row["profit_armed_hours_ago"]))
            sim_pos["profit_armed_at"] = armed_at.isoformat()
        elif gain >= float(life_cfg.get("arm_gain_pct", 3)):
            sim_pos.setdefault("profit_armed_at", (now - timedelta(hours=1)).isoformat())

        market = _build_market(symbol, tf, price, sim_pos, params, rsi=rsi, sim_state=sim_pos)
        rules = _eval_rules(market, sim_pos, params, now=now, coin=coin)
        engine_action = _eval_engine(coin, market, sim_pos, now=now)

        g = _pct(entry, price)
        pk = _pct(entry, float(sim_pos.get("recent_high") or price))
        print(
            f"{row['name']:<42} {g:5.1f}% {pk:5.1f}% "
            f"{rules['technical']:>10} {rules['trail']:>12} {rules['life']:>12} {engine_action:>10}"
        )

    print()
    print("Read-only — no ledger writes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())