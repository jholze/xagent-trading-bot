#!/usr/bin/env python3
"""Local grid backtest: last N days OHLCV for active watchlist coins.

Does not deploy. Fetches public OHLCV via MarketService (Gate/ccxt).

  DEMO_MODE=1 python3 scripts/backtest_grid_watchlist_10d.py
  DEMO_MODE=1 python3 scripts/backtest_grid_watchlist_10d.py --days 10 --tf 4h --limits
  DEMO_MODE=1 python3 scripts/backtest_grid_watchlist_10d.py --watchlist watchlist.demo.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

os.environ.setdefault("DEMO_MODE", "1")


def _load_watchlist_file(path: str) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        coins = data
    else:
        coins = data.get("coins") or []
    return [c for c in coins if isinstance(c, dict) and c.get("active", True) and c.get("symbol")]


def _load_coins(args) -> list[dict]:
    if args.watchlist:
        return _load_watchlist_file(args.watchlist)
    try:
        from data_manager import load_effective_watchlist

        coins = [c for c in load_effective_watchlist() if c.get("active", True)]
        if coins:
            return coins
    except Exception as e:
        print(f"load_effective_watchlist failed: {e}", file=sys.stderr)
    # fallbacks
    for p in ("watchlist.demo.json", "watchlist.json"):
        if Path(p).exists():
            return _load_watchlist_file(p)
    return []


def _bars_for_days(tf: str, days: int) -> int:
    # rough bars needed + buffer
    if tf == "1h":
        return max(days * 24 + 20, 50)
    if tf == "15m":
        return max(days * 24 * 4 + 40, 80)
    # 4h
    return max(days * 6 + 20, 40)


def _atr_pct_from_closes(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 3.0
    # crude ATR% from abs returns
    rets = [abs(closes[i] / closes[i - 1] - 1.0) * 100 for i in range(1, len(closes))]
    window = rets[-period:]
    return max(sum(window) / len(window) * 14, 0.5)  # scale to rough daily-ish


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--days", type=int, default=10)
    p.add_argument("--tf", default="4h", help="OHLCV timeframe")
    p.add_argument("--watchlist", default="", help="JSON path (default: effective / demo)")
    p.add_argument("--cash", type=float, default=10_000.0)
    p.add_argument("--base-buy", type=float, default=500.0)
    p.add_argument("--limits", action="store_true", help="Phase C shadow limits")
    p.add_argument("--max-coins", type=int, default=40)
    args = p.parse_args()

    from intelligence.strategy_backtest import classify_coin
    from intelligence.volatility_classifier import volatility_tier
    from services.market_service import MarketService
    from strategies.grid_plan import simulate_plan_path, spacing_atr_mult_for_coin
    from strategies.grid_limits import simulate_limit_grid_path
    from data_manager import get_config

    coins = _load_coins(args)[: max(1, args.max_coins)]
    if not coins:
        print("ERROR: no watchlist coins", file=sys.stderr)
        return 1

    cfg = get_config()
    va = (cfg or {}).get("volatile_altcoin") or {}
    limit = _bars_for_days(args.tf, args.days)
    market = MarketService()
    sim = simulate_limit_grid_path if args.limits else simulate_plan_path

    print(f"Grid watchlist backtest — last ~{args.days}d · tf={args.tf} · coins={len(coins)}")
    print(f"mode={'limit_shadow' if args.limits else 'market_slices'} · cash/coin=${args.cash:,.0f}")
    print("-" * 88)
    print(
        f"{'symbol':14} {'tier':8} {'class':9} {'bars':>4} "
        f"{'sp×':>5} {'trades':>6} {'equity':>10} {'vsB&H%':>8}"
    )

    rows = []
    for coin in coins:
        sym = coin["symbol"]
        tf = coin.get("timeframe") or args.tf
        try:
            df = market.fetch_ohlcv(sym, args.tf, limit=limit)
        except Exception as e:
            print(f"{sym:14} FETCH_ERR {e}")
            continue
        if df is None or df.empty or "close" not in df.columns:
            print(f"{sym:14} NO_DATA")
            continue
        closes = [float(x) for x in df["close"].tolist() if float(x) > 0]
        # keep last ~days window
        need = _bars_for_days(args.tf, args.days)
        closes = closes[-need:] if len(closes) > need else closes
        if len(closes) < 15:
            print(f"{sym:14} SHORT n={len(closes)}")
            continue

        atr = _atr_pct_from_closes(closes)
        cclass = classify_coin(sym, coin.get("strategy_params") or coin)
        tier = volatility_tier(coin, atr, va)
        spacing = spacing_atr_mult_for_coin(
            volatility_tier=tier, coin_class=cclass, base=0.8,
        )
        res = sim(
            closes,
            symbol=sym,
            timeframe=tf,
            atr_pct=atr,
            spacing_atr_mult=spacing,
            initial_cash=args.cash,
            base_buy_usdt=args.base_buy,
        )
        if res.get("error"):
            print(f"{sym:14} ERR {res['error']}")
            continue
        rows.append({**res, "symbol": sym, "tier": tier, "class": cclass, "spacing": spacing, "bars": len(closes)})
        print(
            f"{sym:14} {tier:8} {cclass:9} {len(closes):4d} "
            f"{spacing:5.2f} {res['trades']:6d} "
            f"${res['final_equity']:9,.0f} {res['vs_buy_hold_pct']:+7.1f}%"
        )

    if not rows:
        print("\nNo successful coin runs.")
        return 1

    # summary
    n = len(rows)
    avg_vs = sum(r["vs_buy_hold_pct"] for r in rows) / n
    med_vs = sorted(r["vs_buy_hold_pct"] for r in rows)[n // 2]
    total_tr = sum(r["trades"] for r in rows)
    beat = sum(1 for r in rows if r["vs_buy_hold_pct"] > 0)
    by_tier: dict[str, list] = {}
    for r in rows:
        by_tier.setdefault(r["tier"], []).append(r)

    print("-" * 88)
    print(f"coins_ok={n}  total_trades={total_tr}  beat_B&H={beat}/{n}")
    print(f"vs B&H: avg={avg_vs:+.2f}%  median={med_vs:+.2f}%")
    for tier, rs in sorted(by_tier.items()):
        a = sum(x["vs_buy_hold_pct"] for x in rs) / len(rs)
        t = sum(x["trades"] for x in rs)
        print(f"  tier={tier:8} n={len(rs)} trades={t} avg_vs_BH={a:+.2f}%")

    # equal-weight portfolio of final equities vs buy-hold
    eq = sum(r["final_equity"] for r in rows)
    bh = sum(r["buy_hold_equity"] for r in rows)
    print(f"portfolio sum equity=${eq:,.0f}  sum B&H=${bh:,.0f}  vs={((eq / bh) - 1) * 100:+.2f}%")
    print("\nLocal only — no deploy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
