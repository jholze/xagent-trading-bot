#!/usr/bin/env python3
"""Portfolio backtest: new exit rules vs actual demo ledger (30 days)."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("DEMO_MODE", "1")

import ccxt

from data_manager import load_orders, load_trade_history_document
from historical_prices import _bars_in_range, _fetch_ohlcv_range, clear_cache
from strategies.positions import load_positions, is_open_position, positions

DAYS = 30
TIMEFRAME = "1h"

# New rules
TRAIL_PCT = 6.0
TRAIL_ARM_GAIN = 15.0
TRAIL_MIN_GAIN = 10.0
TRAIL_FRAC = 0.30
TRAIL_MAX_STEPS = 3
TRAIL_COOLDOWN_H = 6
LIFE_ARM_GAIN = 3.0
LIFE_MAX_H = 96
LIFE_MIN_GAIN = 1.0
LIFE_SKIP_PEAK_ABOVE = 40.0

# Old baseline (comparison)
OLD_TRAIL_PCT = 6.0
OLD_TRAIL_ARM = 6.0
OLD_TRAIL_MIN_GAIN = 0.0


def parse_ts(raw):
    if not raw:
        return None
    try:
        t = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def order_price_usdt(o):
    ex, req = o.get("execution") or {}, o.get("request") or {}
    px = float(ex.get("price") or req.get("price") or 0)
    amt = float(ex.get("amount") or req.get("amount") or 0)
    usdt = ex.get("usdt") or req.get("usdt")
    if usdt is not None:
        return px, float(usdt)
    return px, px * amt


def simulate(
    symbol,
    entry_ts,
    end_ts,
    entry_price,
    entry_usdt,
    mark_price,
    *,
    trail_pct=TRAIL_PCT,
    trail_arm=TRAIL_ARM_GAIN,
    trail_min_gain=TRAIL_MIN_GAIN,
    use_lifetime=True,
    life_max_h=LIFE_MAX_H,
    life_skip_peak_above=LIFE_SKIP_PEAK_ABOVE,
):
    start = entry_ts - timedelta(hours=4)
    end = end_ts + timedelta(hours=48)
    bars = _fetch_ohlcv_range(symbol, start, end, timeframe=TIMEFRAME)
    window = _bars_in_range(bars, entry_ts, end_ts)
    if len(window) < 2:
        return None

    coins0 = entry_usdt / entry_price
    cash, remaining = 0.0, coins0
    recent_high = entry_price
    peak_gain = 0.0
    trail_armed = False
    profit_armed_at = None
    trail_steps = 0
    last_trail_ts = None
    lifetime_hit = False

    for ts_ms, o, high, low, close, v in window:
        bar_ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        high, low, close = float(high), float(low), float(close)
        recent_high = max(recent_high, high)
        peak_gain = max(peak_gain, (recent_high / entry_price - 1) * 100)
        gain_close = (close / entry_price - 1) * 100

        if profit_armed_at is None and gain_close >= LIFE_ARM_GAIN:
            profit_armed_at = bar_ts
        if peak_gain >= trail_arm:
            trail_armed = True

        if trail_armed and remaining > 0 and trail_steps < TRAIL_MAX_STEPS:
            if not last_trail_ts or (bar_ts - last_trail_ts).total_seconds() / 3600 >= TRAIL_COOLDOWN_H:
                drop = (1 - low / recent_high) * 100 if recent_high > 0 else 0
                if drop >= trail_pct and gain_close >= trail_min_gain:
                    sell = remaining if trail_steps >= TRAIL_MAX_STEPS - 1 else remaining * TRAIL_FRAC
                    cash += sell * close
                    remaining -= sell
                    trail_steps += 1
                    last_trail_ts = bar_ts
                    recent_high = close

        if (
            use_lifetime
            and remaining > 0
            and profit_armed_at
            and gain_close >= LIFE_MIN_GAIN
            and peak_gain < life_skip_peak_above
        ):
            if (bar_ts - profit_armed_at).total_seconds() / 3600 >= life_max_h:
                cash += remaining * close
                remaining = 0
                lifetime_hit = True
                break

    return {
        "sim_pnl": cash + remaining * mark_price - entry_usdt,
        "peak_gain_pct": peak_gain,
        "trail_steps": trail_steps,
        "lifetime_hit": lifetime_hit,
        "lifetime_runner": use_lifetime and peak_gain >= life_skip_peak_above,
    }


def position_key(symbol: str, tf: str) -> str:
    return f"{symbol.replace('/', '_')}_{tf}"


def resolve_entry_basis(sym, tf, entry_ts, next_buy_ts, orders, first_px, first_usdt):
    """DCA-aware entry: average_entry from ledger + total buy USDT in cycle."""
    key = position_key(sym, tf)
    if key in positions:
        avg = float(positions[key].get("average_entry") or 0)
        if avg > 0:
            entry_price = avg
        else:
            entry_price = first_px
    else:
        entry_price = first_px

    total_usdt = 0.0
    for o in orders:
        if (o.get("side") or "").lower() != "buy" or o.get("symbol") != sym or (o.get("timeframe") or "4h") != tf:
            continue
        ts = parse_ts((o.get("timestamps") or {}).get("filled") or (o.get("timestamps") or {}).get("created"))
        if not ts or ts < entry_ts:
            continue
        if next_buy_ts and ts >= next_buy_ts:
            continue
        _, usdt = order_price_usdt(o)
        total_usdt += usdt
    return entry_price, total_usdt or first_usdt


def actual_economic(sym, tf, entry_usdt, entry_price, realized_pnl, mark):
    key = position_key(sym, tf)
    if key in positions and is_open_position(positions[key]):
        amt = float(positions[key]["amount"])
        sold = float(positions[key].get("sold_percent", 0) or 0)
        cost_basis = entry_usdt * max(0.0, 1.0 - sold)
        return realized_pnl + amt * mark - cost_basis
    return realized_pnl


def main():
    cutoff = datetime.now(timezone.utc) - timedelta(days=DAYS)
    now = datetime.now(timezone.utc)
    orders = [o for o in load_orders("demo").get("orders", []) if o.get("status") == "filled"]
    orders.sort(
        key=lambda o: parse_ts((o.get("timestamps") or {}).get("filled") or (o.get("timestamps") or {}).get("created"))
        or datetime.min.replace(tzinfo=timezone.utc)
    )

    load_positions("demo")

    keys_active = set()
    for o in orders:
        ts = parse_ts((o.get("timestamps") or {}).get("filled") or (o.get("timestamps") or {}).get("created"))
        if ts and ts >= cutoff:
            keys_active.add((o.get("symbol"), o.get("timeframe") or "4h"))

    lots = []
    for o in orders:
        sym, tf = o.get("symbol"), o.get("timeframe") or "4h"
        if (sym, tf) not in keys_active or (o.get("side") or "").lower() != "buy":
            continue
        ts = parse_ts((o.get("timestamps") or {}).get("filled") or (o.get("timestamps") or {}).get("created"))
        if not ts:
            continue
        px, usdt = order_price_usdt(o)
        if px <= 0 or usdt <= 0:
            continue
        next_buy_ts = None
        for b in orders:
            if (b.get("side") or "").lower() != "buy" or b.get("symbol") != sym or (b.get("timeframe") or "4h") != tf:
                continue
            bts = parse_ts((b.get("timestamps") or {}).get("filled") or (b.get("timestamps") or {}).get("created"))
            if bts and bts > ts:
                next_buy_ts = bts
                break
        sells = []
        for s in orders:
            if (s.get("side") or "").lower() != "sell" or s.get("symbol") != sym or (s.get("timeframe") or "4h") != tf:
                continue
            sts = parse_ts((s.get("timestamps") or {}).get("filled") or (s.get("timestamps") or {}).get("created"))
            if not sts or sts < ts:
                continue
            if next_buy_ts and sts >= next_buy_ts:
                continue
            sells.append(s)
        realized = sum(float(s.get("pnl") or 0) for s in sells)
        last_sell = max((parse_ts((s.get("timestamps") or {}).get("filled")) for s in sells), default=None)
        entry_price, entry_usdt = resolve_entry_basis(sym, tf, ts, next_buy_ts, orders, px, usdt)
        lots.append({
            "symbol": sym,
            "tf": tf,
            "entry_ts": ts,
            "entry_price": entry_price,
            "entry_usdt": entry_usdt,
            "realized": realized,
            "end_ts": last_sell or now,
            "open": last_sell is None,
        })

    seen = set()
    unique = []
    for lot in sorted(lots, key=lambda x: x["entry_ts"]):
        k = (lot["symbol"], lot["tf"])
        if k in seen:
            continue
        seen.add(k)
        unique.append(lot)

    ex = ccxt.gate({"enableRateLimit": True})
    prices = {}
    for sym in {l["symbol"] for l in unique}:
        try:
            prices[sym] = float(ex.fetch_ticker(sym)["last"])
        except Exception:
            prices[sym] = 0.0

    rows = []
    skipped = 0
    for lot in unique:
        mark = prices.get(lot["symbol"], 0)
        if mark <= 0:
            skipped += 1
            continue
        end = now if lot["open"] else lot["end_ts"]
        clear_cache()
        new = simulate(
            lot["symbol"], lot["entry_ts"], end, lot["entry_price"], lot["entry_usdt"], mark,
            use_lifetime=True,
        )
        old = simulate(
            lot["symbol"], lot["entry_ts"], end, lot["entry_price"], lot["entry_usdt"], mark,
            trail_pct=OLD_TRAIL_PCT,
            trail_arm=OLD_TRAIL_ARM,
            trail_min_gain=OLD_TRAIL_MIN_GAIN,
            use_lifetime=False,
        )
        if not new or not old:
            skipped += 1
            continue
        actual = actual_economic(
            lot["symbol"], lot["tf"], lot["entry_usdt"], lot["entry_price"], lot["realized"], mark,
        )
        rows.append({**lot, **new, "old_pnl": old["sim_pnl"], "actual": actual, "mark": mark})

    realized_30d = sum(
        float(o.get("pnl") or 0)
        for o in orders
        if (o.get("side") or "").lower() == "sell"
        and (t := parse_ts((o.get("timestamps") or {}).get("filled") or (o.get("timestamps") or {}).get("created")))
        and t >= cutoff
    )

    n = len(rows)
    actual_eco = sum(r["actual"] for r in rows)
    new_eco = sum(r["sim_pnl"] for r in rows)
    old_eco = sum(r["old_pnl"] for r in rows)

    print("=" * 72)
    print(f"PORTFOLIO 30d — {n} Positionen ({skipped} übersprungen)")
    print(
        "NEU: Trail 6% arm@15% exit@10% + Profit-Lifetime 96h "
        f"(ab +3%, nur im Plus, Runner-Skip ab +{LIFE_SKIP_PEAK_ABOVE:.0f}%)"
    )
    print("=" * 72)
    print(f"Realisiert (alle Sells 30d):     ${realized_30d:>10,.0f}")
    print(f"Actual economic (MTM+realized): ${actual_eco:>10,.0f}")
    print(f"Sim NEUE Regeln:                ${new_eco:>10,.0f}  (Δ {new_eco - actual_eco:+,.0f})")
    print(f"Sim ALTE 6% base (ohne life):   ${old_eco:>10,.0f}  (Δ {old_eco - actual_eco:+,.0f})")
    print(f"Lifetime-Forces: {sum(1 for r in rows if r['lifetime_hit'])}/{n}")
    print(f"Runner-Skip:     {sum(1 for r in rows if r.get('lifetime_runner'))}/{n}")
    print(f"Ø Trail-Steps:   {sum(r['trail_steps'] for r in rows)/n:.1f}")
    print(f"NEU besser:      {sum(1 for r in rows if r['sim_pnl'] > r['actual'] + 5)}/{n}")
    print(f"NEU schlechter:  {sum(1 for r in rows if r['sim_pnl'] < r['actual'] - 5)}/{n}")
    print()

    print("Top Δ NEU vs Actual:")
    for r in sorted(rows, key=lambda x: x["sim_pnl"] - x["actual"], reverse=True)[:10]:
        d = r["sim_pnl"] - r["actual"]
        print(
            f"  {r['symbol']:14} act=${r['actual']:7.0f} neu=${r['sim_pnl']:7.0f} "
            f"Δ={d:+6.0f} peak={r['peak_gain_pct']:5.0f}% t={r['trail_steps']} life={r['lifetime_hit']}"
        )
    print("\nBottom Δ NEU vs Actual:")
    for r in sorted(rows, key=lambda x: x["sim_pnl"] - x["actual"])[:8]:
        d = r["sim_pnl"] - r["actual"]
        print(
            f"  {r['symbol']:14} act=${r['actual']:7.0f} neu=${r['sim_pnl']:7.0f} "
            f"Δ={d:+6.0f} peak={r['peak_gain_pct']:5.0f}% t={r['trail_steps']} life={r['lifetime_hit']}"
        )

    hist = load_trade_history_document("demo")
    print(f"\nLedger: cash=${float(hist.get('virtual_balance', 0)):,.0f} realized=${float(hist.get('realized_pnl', 0)):,.0f}")


if __name__ == "__main__":
    main()