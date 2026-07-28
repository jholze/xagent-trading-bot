#!/usr/bin/env python3
"""Fast 10-day exit-policy A/B on open demo positions (OHLCV sim).

Optimized: one OHLCV fetch per symbol, all variants share bars, no cache clears.
Focus: currently open positions (what matters for rotation).

  DEMO_MODE=1 MONGO_URL=... MONGODB_DB=xagent_test \\
    python3.13 scripts/backtest_exit_policy_10d.py --days 10 --tenants default,henry
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEMO_MODE", "1")
os.environ.setdefault("DEMO_LEDGER_BACKEND", "mongo")

import ccxt  # noqa: E402

from historical_prices import _bars_in_range, _fetch_ohlcv_range  # noqa: E402

TIMEFRAME = "1h"

VARIANTS: dict[str, dict] = {
    "base": {
        "trail_pct": 6.0,
        "trail_arm": 15.0,
        "trail_min_gain": 10.0,
        "life_arm": 3.0,
        "life_max_h": 96.0,
        "life_min_gain": 1.0,
        "life_skip_peak": 40.0,
        "use_lifetime": True,
        "trail_max_steps": 1,
        "trail_cooldown_h": 6.0,
    },
    "rot_mid": {
        "trail_pct": 6.0,
        "trail_arm": 10.0,
        "trail_min_gain": 6.0,
        "life_arm": 3.0,
        "life_max_h": 48.0,
        "life_min_gain": 1.0,
        "life_skip_peak": 40.0,
        "use_lifetime": True,
        "trail_max_steps": 1,
        "trail_cooldown_h": 6.0,
    },
    "rot_agg": {
        "trail_pct": 5.0,
        "trail_arm": 8.0,
        "trail_min_gain": 5.0,
        "life_arm": 3.0,
        "life_max_h": 24.0,
        "life_min_gain": 1.0,
        "life_skip_peak": 40.0,
        "use_lifetime": True,
        "trail_max_steps": 1,
        "trail_cooldown_h": 4.0,
    },
}


def parse_ts(raw) -> datetime | None:
    if not raw:
        return None
    try:
        t = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except Exception:
        return None


@dataclass
class SimResult:
    sim_pnl: float
    peak_gain_pct: float
    exit_reason: str
    hold_h: float
    closed: bool


def simulate_bars(
    bars: list,
    entry_ts: datetime,
    end_ts: datetime,
    entry_price: float,
    entry_usdt: float,
    mark_price: float,
    cfg: dict,
) -> SimResult | None:
    window = _bars_in_range(bars, entry_ts, end_ts)
    if len(window) < 2:
        # fallback: synthetic 2-point if no bars
        if entry_price <= 0 or mark_price <= 0:
            return None
        gain = (mark_price / entry_price - 1) * 100
        return SimResult(
            sim_pnl=entry_usdt * (mark_price / entry_price - 1),
            peak_gain_pct=max(0.0, gain),
            exit_reason="open",
            hold_h=max(0.0, (end_ts - entry_ts).total_seconds() / 3600),
            closed=False,
        )

    coins0 = entry_usdt / entry_price if entry_price > 0 else 0.0
    if coins0 <= 0:
        return None
    cash, remaining = 0.0, coins0
    recent_high = entry_price
    peak_gain = 0.0
    trail_armed = False
    profit_armed_at = None
    trail_steps = 0
    last_trail_ts = None
    exit_reason = "open"
    exit_ts = end_ts

    trail_pct = float(cfg["trail_pct"])
    trail_arm = float(cfg["trail_arm"])
    trail_min_gain = float(cfg["trail_min_gain"])
    life_arm = float(cfg["life_arm"])
    life_max_h = float(cfg["life_max_h"])
    life_min_gain = float(cfg["life_min_gain"])
    life_skip = float(cfg["life_skip_peak"])
    use_life = bool(cfg["use_lifetime"])
    trail_max_steps = int(cfg["trail_max_steps"])
    cooldown = float(cfg["trail_cooldown_h"])

    for ts_ms, _o, high, low, close, _v in window:
        bar_ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        high, low, close = float(high), float(low), float(close)
        recent_high = max(recent_high, high)
        peak_gain = max(peak_gain, (recent_high / entry_price - 1) * 100)
        gain_close = (close / entry_price - 1) * 100

        if profit_armed_at is None and gain_close >= life_arm:
            profit_armed_at = bar_ts
        if peak_gain >= trail_arm:
            trail_armed = True

        if trail_armed and remaining > 0 and trail_steps < trail_max_steps:
            ok_cd = (
                not last_trail_ts
                or (bar_ts - last_trail_ts).total_seconds() / 3600 >= cooldown
            )
            if ok_cd:
                drop = (1 - low / recent_high) * 100 if recent_high > 0 else 0
                if drop >= trail_pct and gain_close >= trail_min_gain:
                    cash += remaining * close
                    remaining = 0
                    trail_steps += 1
                    exit_reason = "trail"
                    exit_ts = bar_ts
                    break

        if (
            use_life
            and remaining > 0
            and profit_armed_at
            and gain_close >= life_min_gain
            and peak_gain < life_skip
        ):
            if (bar_ts - profit_armed_at).total_seconds() / 3600 >= life_max_h:
                cash += remaining * close
                remaining = 0
                exit_reason = "lifetime"
                exit_ts = bar_ts
                break

    if remaining > 0:
        cash += remaining * mark_price
        exit_reason = "open"
        exit_ts = end_ts

    hold_h = max(0.0, (exit_ts - entry_ts).total_seconds() / 3600)
    return SimResult(
        sim_pnl=cash - entry_usdt,
        peak_gain_pct=peak_gain,
        exit_reason=exit_reason,
        hold_h=hold_h,
        closed=exit_reason != "open",
    )


def mongo_client():
    uri = (
        os.environ.get("MONGO_URL")
        or os.environ.get("MONGO_PUBLIC_URL")
        or os.environ.get("MONGODB_URI")
        or ""
    )
    if not uri:
        raise SystemExit("Set MONGO_URL or MONGO_PUBLIC_URL")
    from pymongo import MongoClient

    return MongoClient(uri, serverSelectionTimeoutMS=15000)


def load_open_lots(tenant_id: str, scope: str = "demo") -> list[dict]:
    """Open positions from positions blob — fast path."""
    client = mongo_client()
    dbn = os.environ.get("MONGODB_DB", "xagent_test")
    db = client[dbn]
    doc = db["positions"].find_one({"_id": f"{tenant_id}:{scope}"})
    if not doc:
        doc = db["positions"].find_one({"tenant_id": tenant_id, "ledger_scope": scope})
    pos = (doc or {}).get("positions") or {}
    lots = []
    for key, p in pos.items():
        if not isinstance(p, dict):
            continue
        amt = float(p.get("amount") or 0)
        sold = float(p.get("sold_percent") or 0)
        if amt <= 1e-12 or sold >= 0.999:
            continue
        # key like ORDI_USDT_1h
        parts = key.rsplit("_", 1)
        if len(parts) != 2:
            continue
        base, tf = parts
        symbol = base.replace("_", "/", 1) if "_" in base else base
        # ORDI_USDT -> ORDI/USDT
        if "_" in base:
            a, b = base.rsplit("_", 1)
            symbol = f"{a}/{b}"
        entry = float(p.get("average_entry") or 0)
        if entry <= 0:
            continue
        entry_ts = (
            parse_ts(p.get("entry_at"))
            or parse_ts(p.get("first_buy_at"))
            or parse_ts(p.get("last_trade_at"))
        )
        if not entry_ts:
            entry_ts = datetime.now(timezone.utc) - timedelta(days=10)
        usdt = amt * entry
        lots.append(
            {
                "symbol": symbol,
                "tf": tf,
                "entry_ts": entry_ts,
                "entry_price": entry,
                "entry_usdt": usdt,
                "amount": amt,
                "recent_high": float(p.get("recent_high") or 0),
                "tier": p.get("strategy_tier") or "",
            }
        )
    return lots


def run_tenant(tenant_id: str, days: int, now: datetime, max_coins: int = 0) -> dict:
    t0 = time.time()
    print(f"\n{'='*72}\nTENANT {tenant_id}\n{'='*72}", flush=True)
    lots = load_open_lots(tenant_id)
    if max_coins > 0:
        lots = lots[:max_coins]
    print(f"open lots: {len(lots)}", flush=True)

    # mark prices — parallel (spot only)
    ex = ccxt.gate({"enableRateLimit": False, "options": {"defaultType": "spot"}})
    prices: dict[str, float] = {}
    syms = sorted({l["symbol"] for l in lots})

    def _ticker(sym: str) -> tuple[str, float]:
        try:
            return sym, float(ex.fetch_ticker(sym)["last"])
        except Exception:
            return sym, 0.0

    print(f"fetching {len(syms)} tickers (parallel)…", flush=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        for sym, px in pool.map(_ticker, syms):
            prices[sym] = px
    print(f"  tickers done ok={sum(1 for v in prices.values() if v>0)}/{len(syms)}", flush=True)

    # OHLCV once per symbol — parallel, last `days` only
    sim_start_global = now - timedelta(days=days)
    ohlcv: dict[str, list] = {}

    def _ohlcv_one(sym: str) -> tuple[str, list]:
        ents = [l["entry_ts"] for l in lots if l["symbol"] == sym]
        start = min(ents) if ents else sim_start_global
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        start = max(start, sim_start_global) - timedelta(hours=6)
        try:
            return sym, _fetch_ohlcv_range(sym, start, now, timeframe=TIMEFRAME)
        except Exception:
            return sym, []

    print(f"fetching OHLCV 1h (~{days}d, parallel)…", flush=True)
    done = 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = {pool.submit(_ohlcv_one, s): s for s in syms}
        for fut in as_completed(futs):
            sym, bars = fut.result()
            ohlcv[sym] = bars
            done += 1
            if done % 10 == 0 or done == len(syms):
                print(f"  ohlcv {done}/{len(syms)} last={sym} bars={len(bars)}", flush=True)

    rows = []
    for lot in lots:
        sym = lot["symbol"]
        mark = prices.get(sym) or 0.0
        if mark <= 0:
            continue
        # sim window: max(entry, now-days) → now
        entry_ts = lot["entry_ts"]
        if entry_ts.tzinfo is None:
            entry_ts = entry_ts.replace(tzinfo=timezone.utc)
        win_start = max(entry_ts, sim_start_global)
        bars = ohlcv.get(sym) or []
        actual = lot["amount"] * mark - lot["entry_usdt"]
        sims = {}
        for name, cfg in VARIANTS.items():
            r = simulate_bars(
                bars,
                win_start,
                now,
                lot["entry_price"],
                lot["entry_usdt"],
                mark,
                cfg,
            )
            if r is None:
                r = SimResult(actual, 0.0, "open", 0.0, False)
            sims[name] = r
        best = max(sims.keys(), key=lambda n: sims[n].sim_pnl)
        rows.append(
            {
                "symbol": sym,
                "tf": lot["tf"],
                "tier": lot.get("tier") or "",
                "entry_usdt": round(lot["entry_usdt"], 2),
                "mark": mark,
                "actual_mtm": round(actual, 2),
                "peak_gain": round(sims["base"].peak_gain_pct, 1),
                "best": best,
                "delta_mid_vs_base": round(
                    sims["rot_mid"].sim_pnl - sims["base"].sim_pnl, 2
                ),
                **{f"{n}_pnl": round(sims[n].sim_pnl, 2) for n in VARIANTS},
                **{f"{n}_exit": sims[n].exit_reason for n in VARIANTS},
                **{f"{n}_hold_h": round(sims[n].hold_h, 1) for n in VARIANTS},
            }
        )

    n = len(rows)
    if n == 0:
        print("no rows")
        return {"tenant": tenant_id, "rows": [], "summary": {}}

    sum_act = sum(r["actual_mtm"] for r in rows)
    sums = {nme: sum(r[f"{nme}_pnl"] for r in rows) for nme in VARIANTS}
    closed = {
        nme: sum(1 for r in rows if r[f"{nme}_exit"] != "open") for nme in VARIANTS
    }
    mid_wins = sum(1 for r in rows if r["rot_mid_pnl"] > r["base_pnl"] + 5)
    agg_wins = sum(1 for r in rows if r["rot_agg_pnl"] > r["base_pnl"] + 5)

    print(f"\nOpen positions simulated: {n}  ({time.time()-t0:.0f}s)")
    print(f"{'variant':10} {'Σ MTM/pnl':>12} {'would_close':>12}")
    print(f"{'actual':10} {sum_act:12.0f} {'(still open)':>12}")
    for nme in VARIANTS:
        print(
            f"{nme:10} {sums[nme]:12.0f} {closed[nme]:12d}  "
            f"Δ vs actual {sums[nme]-sum_act:+.0f}"
        )
    print(f"\nrot_mid better than base (>+$5): {mid_wins}/{n}")
    print(f"rot_agg better than base (>+$5): {agg_wins}/{n}")

    print("\nTop Δ rot_mid − base (would have helped):")
    for r in sorted(rows, key=lambda x: x["delta_mid_vs_base"], reverse=True)[:15]:
        print(
            f"  {r['symbol']:14} act=${r['actual_mtm']:7.0f} "
            f"base=${r['base_pnl']:7.0f} mid=${r['rot_mid_pnl']:7.0f} "
            f"agg=${r['rot_agg_pnl']:7.0f} Δmid={r['delta_mid_vs_base']:+6.0f} "
            f"exit={r['rot_mid_exit']:8} peak={r['peak_gain']:5.0f}% tier={r['tier']}"
        )

    print("\nWhere rot_mid hurts (left money on table / early exit):")
    hurt = [r for r in rows if r["delta_mid_vs_base"] < -5]
    for r in sorted(hurt, key=lambda x: x["delta_mid_vs_base"])[:10]:
        print(
            f"  {r['symbol']:14} base=${r['base_pnl']:7.0f} mid=${r['rot_mid_pnl']:7.0f} "
            f"Δ={r['delta_mid_vs_base']:+6.0f} mid_exit={r['rot_mid_exit']} peak={r['peak_gain']:.0f}%"
        )
    if not hurt:
        print("  (none material)")

    # would-close now under mid
    would = [r for r in rows if r["rot_mid_exit"] != "open"]
    print(f"\nWould already be CLOSED under rot_mid: {len(would)}/{n}")
    for r in would[:20]:
        print(
            f"  {r['symbol']:14} via {r['rot_mid_exit']:8} "
            f"sim_pnl=${r['rot_mid_pnl']:.0f} (actual still open MTM ${r['actual_mtm']:.0f})"
        )

    return {
        "tenant": tenant_id,
        "rows": rows,
        "summary": {
            "n": n,
            "sum_actual": round(sum_act, 2),
            "sums": {k: round(v, 2) for k, v in sums.items()},
            "closed": closed,
            "rot_mid_wins": mid_wins,
            "rot_agg_wins": agg_wins,
            "elapsed_s": round(time.time() - t0, 1),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--tenants", default="default,henry")
    ap.add_argument("--max-coins", type=int, default=0, help="cap for smoke tests")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    tenants = [t.strip() for t in args.tenants.split(",") if t.strip()]
    reports = [run_tenant(t, args.days, now, args.max_coins) for t in tenants]

    out = args.out
    if not out:
        stamp = now.strftime("%Y-%m-%d_%H%M")
        out_dir = ROOT / "auswertungen"
        out_dir.mkdir(exist_ok=True)
        out = str(out_dir / f"exit_policy_10d_{stamp}.json")
    Path(out).write_text(
        json.dumps(
            {"generated_at": now.isoformat(), "days": args.days, "variants": VARIANTS, "tenants": reports},
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nWrote {out}")
    print("\nCOMBINED:")
    for rep in reports:
        s = rep.get("summary") or {}
        if not s:
            continue
        print(
            f"  {rep['tenant']}: n={s.get('n')} "
            f"actual=${s.get('sum_actual', 0):.0f} "
            f"base=${s.get('sums', {}).get('base', 0):.0f} "
            f"mid=${s.get('sums', {}).get('rot_mid', 0):.0f} "
            f"agg=${s.get('sums', {}).get('rot_agg', 0):.0f} "
            f"mid_closes={s.get('closed', {}).get('rot_mid')} "
            f"({s.get('elapsed_s')}s)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
