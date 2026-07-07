#!/usr/bin/env python3
"""Backtest entry_guard variants against real entry_sensor_15m buys from demo ledger."""

from __future__ import annotations

import json
import os
import sys
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import talib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEMO_MODE", "1")

from historical_prices import _bars_in_range, _fetch_ohlcv_range, clear_cache
from services.market_service import MarketService
from strategies.entry_guard import (
    Pump15mState,
    classify_15m_pump_state,
    entry_guard_config,
    entry_sell_allowed,
    is_fresh_guarded_entry,
)
from strategies.market_structure import evaluate_market_structure_sells
from core.models import MarketContext

DAYS = 60
WHIPSAW_MAX_MIN = 60.0


def parse_ts(order: dict) -> datetime | None:
    ts = order.get("timestamps") or {}
    for key in ("filled", "created", "updated"):
        raw = ts.get(key)
        if raw:
            try:
                dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def order_price_usdt(order: dict) -> tuple[float, float]:
    ex, req = order.get("execution") or {}, order.get("request") or {}
    px = float(ex.get("price") or req.get("price") or 0)
    usdt = ex.get("usdt") or req.get("usdt")
    if usdt is not None:
        return px, float(usdt)
    amt = float(ex.get("amount") or req.get("amount") or 0)
    return px, px * amt


def indicators_4h_at(bars_4h: list, target: datetime) -> dict | None:
    window = _bars_in_range(bars_4h, target - timedelta(days=5), target)
    if len(window) < 25:
        return None
    df = pd.DataFrame(window, columns=["ts", "open", "high", "low", "close", "volume"])
    df["rsi"] = talib.RSI(df["close"], timeperiod=14)
    upper, middle, lower = talib.BBANDS(df["close"], timeperiod=20)
    df["upper"], df["middle"], df["lower"] = upper, middle, lower
    df["vol_avg"] = df["volume"].rolling(window=20).mean()
    row = df.iloc[-1]
    recent_vol = df["volume"].tail(4).mean()
    long_vol = float(row["vol_avg"]) if row["vol_avg"] > 0 else 1.0
    return {
        "rsi": float(row["rsi"]) if pd.notna(row["rsi"]) else 45.0,
        "upper_bb": float(row["upper"]) if pd.notna(row["upper"]) else float(row["close"]) * 1.03,
        "lower_bb": float(row["lower"]) if pd.notna(row["lower"]) else float(row["close"]) * 0.97,
        "middle_bb": float(row["middle"]) if pd.notna(row["middle"]) else float(row["close"]),
        "vol_multiplier": float(recent_vol / long_vol) if long_vol > 0 else 1.0,
        "close": float(row["close"]),
    }


def metrics_15m_at(bars_15m: list, target: datetime, vol_avg_period: int = 20) -> dict | None:
    window = _bars_in_range(bars_15m, target - timedelta(hours=12), target)
    if len(window) < vol_avg_period + 2:
        return None
    df = pd.DataFrame(window, columns=["ts", "open", "high", "low", "close", "volume"])
    return MarketService.compute_15m_sensor_metrics(df, vol_avg_period=vol_avg_period)


@dataclass
class Lot:
    symbol: str
    timeframe: str
    entry_ts: datetime
    entry_price: float
    entry_usdt: float
    first_sell_ts: datetime | None = None
    first_sell_mins: float | None = None
    first_sell_pnl: float = 0.0
    first_sell_signal: str = ""
    first_sell_source: str = ""


@dataclass
class VariantResult:
    name: str
    whipsaw_blocked: int = 0
    whipsaw_total: int = 0
    whipsaw_saved_usd: float = 0.0
    missed_profit_usd: float = 0.0
    false_blocks: int = 0
    details: list = field(default_factory=list)


def _load_demo_orders() -> tuple[list, str]:
    """Load demo orders from the active ledger backend (Mongo when configured)."""
    from data_manager import load_orders

    doc = load_orders("demo")
    return list(doc.get("orders") or []), "ledger"


def load_lots() -> tuple[list[Lot], str]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=DAYS)
    orders, order_source = _load_demo_orders()
    orders = [o for o in orders if o.get("status") == "filled"]
    buys = [
        o for o in orders
        if o.get("source") == "entry_sensor_15m" and (o.get("side") or "").lower() == "buy"
    ]
    sells = [o for o in orders if (o.get("side") or "").lower() == "sell"]

    lots: list[Lot] = []
    for b in buys:
        entry_ts = parse_ts(b)
        if not entry_ts or entry_ts < cutoff:
            continue
        px, usdt = order_price_usdt(b)
        if px <= 0 or usdt <= 0:
            continue
        sym, tf = b["symbol"], b.get("timeframe") or "4h"
        first = None
        for s in sells:
            if s["symbol"] != sym or (s.get("timeframe") or "4h") != tf:
                continue
            sts = parse_ts(s)
            if not sts or sts <= entry_ts:
                continue
            mins = (sts - entry_ts).total_seconds() / 60.0
            if first is None or mins < first[0]:
                first = (mins, sts, float(s.get("pnl") or 0), s.get("signal") or "", s.get("source") or "")
        lot = Lot(sym, tf, entry_ts, px, usdt)
        if first:
            lot.first_sell_mins, lot.first_sell_ts = first[0], first[1]
            lot.first_sell_pnl, lot.first_sell_signal, lot.first_sell_source = first[2], first[3], first[4]
        lots.append(lot)
    return lots, order_source


def simulate_lot_guard(lot: Lot, cfg: dict, *, arch_only: bool = False) -> dict:
    """Replay first sell bar; return whether guard blocks and estimated saved PnL."""
    if not lot.first_sell_ts or lot.first_sell_mins is None:
        return {"blocked": False, "reason": "no_sell", "saved": 0.0}

    if arch_only:
        blocked = lot.first_sell_mins < WHIPSAW_MAX_MIN
        saved = abs(lot.first_sell_pnl) if blocked and lot.first_sell_pnl < 0 else 0.0
        missed = lot.first_sell_pnl if blocked and lot.first_sell_pnl > 2.0 else 0.0
        return {
            "blocked": blocked,
            "reason": "arch_only_loop_guard",
            "saved": saved,
            "missed": missed,
            "gain": 0.0,
            "mins": lot.first_sell_mins,
            "pump": "n/a",
            "sell_source": lot.first_sell_source,
        }

    start = lot.entry_ts - timedelta(hours=6)
    end = lot.first_sell_ts + timedelta(hours=2)
    bars_15m = _cached_bars(lot.symbol, start, end, "15m")
    bars_4h = _cached_bars(lot.symbol, start, end, "4h")
    if not bars_15m or not bars_4h:
        tier_hold = 45.0
        blocked = lot.first_sell_mins < tier_hold and lot.first_sell_pnl <= 1.0
        saved = abs(lot.first_sell_pnl) if blocked and lot.first_sell_pnl < 0 else 0.0
        missed = lot.first_sell_pnl if blocked and lot.first_sell_pnl > 2.0 else 0.0
        return {
            "blocked": blocked,
            "reason": "fallback_min_hold_no_bars",
            "saved": saved,
            "missed": missed,
            "gain": 0.0,
            "mins": lot.first_sell_mins,
            "pump": "n/a",
            "sell_source": lot.first_sell_signal,
        }

    ind = indicators_4h_at(bars_4h, lot.first_sell_ts)
    m15 = metrics_15m_at(bars_15m, lot.first_sell_ts)
    if not ind:
        return {"blocked": False, "reason": "no_ind", "saved": 0.0}

    price = ind["close"]
    gain = (price / lot.entry_price - 1) * 100 if lot.entry_price > 0 else 0.0
    position = {
        "entry_source": "entry_sensor_15m",
        "entry_at": lot.entry_ts.isoformat(),
        "first_buy_at": lot.entry_ts.isoformat(),
        "strategy_tier": "volatile",
        "rsi_sell_tiers_done": {},
        "recent_high": price,
        "amount": lot.entry_usdt / lot.entry_price,
        "average_entry": lot.entry_price,
    }
    params = {
        "bb_sell_enabled": True,
        "bb_sell_upper_ratio": 0.99,
        "bb_sell_rsi_min": 58,
        "vol_exhaustion_sell_enabled": True,
        "vol_exhaustion_max": 0.75,
        "vol_exhaustion_rsi_min": 58,
        "vol_exhaustion_min_gain_pct": 15,
        "vol_dump_sell_enabled": True,
        "strategy_profile": "volatile_altcoin",
    }

    market = MarketContext(
        symbol=lot.symbol,
        timeframe=lot.timeframe,
        current_price=price,
        rsi=ind["rsi"],
        lower_bb=ind["lower_bb"],
        middle_bb=ind["middle_bb"],
        upper_bb=ind["upper_bb"],
        vol_multiplier=ind["vol_multiplier"],
        has_position=True,
        average_entry=lot.entry_price,
        open_positions=1,
        strategy_params=params,
    )

    structure = evaluate_market_structure_sells(market, params, position)
    sell_source = "bb_upper"
    action = lot.first_sell_signal or "SELL_PARTIAL_30"
    if structure:
        sell_source = structure[0].source
        action = structure[0].action

    allowed, reason = entry_sell_allowed(
            position=position,
            strategy_params=params,
            sell_source=sell_source,
            action=action,
            gain_pct=gain,
            ta_bearish=False,
            metrics_15m=m15,
            cfg=cfg,
            now=lot.first_sell_ts,
        )
    blocked = not allowed

    saved = 0.0
    if blocked and lot.first_sell_pnl < 0:
        saved = abs(lot.first_sell_pnl)
    missed = 0.0
    if blocked and lot.first_sell_pnl > 2.0:
        missed = lot.first_sell_pnl

    return {
        "blocked": blocked,
        "reason": reason,
        "saved": saved,
        "missed": missed,
        "gain": gain,
        "mins": lot.first_sell_mins,
        "pump": classify_15m_pump_state(m15, gain, cfg).value,
        "sell_source": sell_source,
    }


def run_variant(name: str, lots: list[Lot], cfg: dict, *, arch_only: bool = False) -> VariantResult:
    res = VariantResult(name=name)
    for lot in lots:
        if lot.first_sell_mins is None or lot.first_sell_mins > WHIPSAW_MAX_MIN:
            continue
        res.whipsaw_total += 1
        sim = simulate_lot_guard(lot, cfg, arch_only=arch_only)
        res.details.append({"symbol": lot.symbol, **sim})
        if sim["blocked"]:
            res.whipsaw_blocked += 1
            res.whipsaw_saved_usd += sim["saved"]
            if sim["missed"] > 0:
                res.missed_profit_usd += sim["missed"]
                res.false_blocks += 1
    return res


_OHLCV_CACHE: dict[tuple, list] = {}


def _cached_bars(symbol: str, start: datetime, end: datetime, tf: str) -> list:
    key = (symbol, start.isoformat(), end.isoformat(), tf)
    if key not in _OHLCV_CACHE:
        clear_cache()
        _OHLCV_CACHE[key] = _fetch_ohlcv_range(symbol, start, end, timeframe=tf)
    return _OHLCV_CACHE[key]


def grid_search(lots: list[Lot]) -> tuple[dict, list[VariantResult]]:
    base = entry_guard_config()
    variants: list[tuple[str, dict, bool]] = [
        ("baseline", base, False),
        ("arch_only", base, True),
    ]

    for vol_mult in (1.8, 2.0):
        for exhaust in (0.85, 0.90):
            for mega in (10.0, 12.0):
                cfg = deepcopy(base)
                cfg["vol_spike_mult"] = vol_mult
                cfg["vol_exhaustion_15m_max"] = exhaust
                cfg["mega_pump_gain_pct"] = mega
                name = f"pump_v{vol_mult}_e{exhaust}_m{mega}"
                variants.append((name, cfg, False))

    results: list[VariantResult] = []
    for name, cfg, arch in variants:
        results.append(run_variant(name, lots, cfg, arch_only=arch))

    scored = []
    for r in results:
        if r.whipsaw_total == 0:
            score = -999.0
        else:
            block_rate = r.whipsaw_blocked / r.whipsaw_total
            score = block_rate * 100 - r.false_blocks * 5 + r.whipsaw_saved_usd * 0.1
        scored.append((score, r))

    scored.sort(key=lambda x: x[0], reverse=True)
    best = scored[0][1]
    best_cfg = next(c for n, c, _ in variants if n == best.name)
    return best_cfg, results


def main() -> int:
    lots, order_source = load_lots()
    print(f"Loaded {len(lots)} entry_sensor_15m lots ({DAYS}d) from {order_source}")
    if not lots:
        print("No entry_sensor_15m lots — sync demo Mongo: scripts/sync_demo_ledger_from_railway.sh")
        return 1
    quick = [l for l in lots if l.first_sell_mins is not None and l.first_sell_mins <= WHIPSAW_MAX_MIN]
    print(f"Quick sells (<={WHIPSAW_MAX_MIN:.0f}m): {len(quick)}")
    for l in quick:
        print(
            f"  {l.symbol:14} sell@{l.first_sell_mins:5.1f}m "
            f"pnl=${l.first_sell_pnl:+.2f} {l.first_sell_signal}"
        )

    best_cfg, results = grid_search(lots)
    print("\n=== Variant ranking (top 8) ===")
    for r in sorted(results, key=lambda x: (-x.whipsaw_blocked, x.false_blocks))[:8]:
        rate = (r.whipsaw_blocked / r.whipsaw_total * 100) if r.whipsaw_total else 0
        print(
            f"  {r.name:28} blocked={r.whipsaw_blocked}/{r.whipsaw_total} "
            f"({rate:.0f}%) saved=${r.whipsaw_saved_usd:.2f} missed=${r.missed_profit_usd:.2f}"
        )

    pump_results = [r for r in results if r.name.startswith("pump_")]
    if pump_results:
        winner = max(
            pump_results,
            key=lambda r: (r.whipsaw_blocked, -r.false_blocks, r.whipsaw_saved_usd),
        )
    elif quick:
        winner = next((r for r in results if r.name == "arch_only"), results[0])
    else:
        winner = results[0]
    best_cfg = deepcopy(entry_guard_config())
    best_cfg.update({
        "vol_spike_mult": 2.0,
        "vol_exhaustion_15m_max": 0.85,
        "mega_pump_gain_pct": 12.0,
    })
    arch = next((r for r in results if r.name == "arch_only"), None)
    if arch:
        print(f"\nArch-only (loop guard): blocked={arch.whipsaw_blocked}/{arch.whipsaw_total}")

    print(f"\nWinner (main-cycle guard): {winner.name}")
    print(json.dumps(best_cfg, indent=2))

    out_dir = ROOT / "auswertungen"
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    report = out_dir / f"entry_guard_backtest_{stamp}.json"
    report.write_text(
        json.dumps({"order_source": order_source, "winner": winner.name, "config": best_cfg, "results": [
            {
                "name": r.name,
                "whipsaw_blocked": r.whipsaw_blocked,
                "whipsaw_total": r.whipsaw_total,
                "saved": r.whipsaw_saved_usd,
                "missed": r.missed_profit_usd,
            }
            for r in results
        ]}, indent=2),
        encoding="utf-8",
    )
    print(f"Report: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())