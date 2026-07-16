#!/usr/bin/env python3
"""Local grid-plan backtest (Phase A) — no Railway deploy required.

Examples:
  python3 scripts/backtest_grid_plan.py
  python3 scripts/backtest_grid_plan.py --scenario ranging
  python3 scripts/backtest_grid_plan.py --ohlcv path/to.csv --col close
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from strategies.grid_plan import simulate_plan_path  # noqa: E402
from strategies.grid_limits import simulate_limit_grid_path  # noqa: E402


def _series_ranging(n: int = 200, mid: float = 100.0, amp: float = 8.0) -> list[float]:
    out = []
    for i in range(n):
        out.append(mid + amp * math.sin(i / 7.0) + 0.3 * math.sin(i / 2.3))
    return out


def _series_uptrend(n: int = 200, start: float = 100.0, step: float = 0.35) -> list[float]:
    return [start + i * step + 0.5 * math.sin(i / 4.0) for i in range(n)]


def _series_crash(n: int = 120, start: float = 100.0) -> list[float]:
    return [start * (0.992 ** i) + 0.2 * math.sin(i) for i in range(n)]


def _load_csv(path: str, col: str = "close") -> list[float]:
    prices: list[float] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or col not in reader.fieldnames:
            # plain single-column
            f.seek(0)
            for line in f:
                line = line.strip()
                if not line or line.lower().startswith("close"):
                    continue
                try:
                    prices.append(float(line.split(",")[0]))
                except ValueError:
                    continue
            return prices
        for row in reader:
            try:
                prices.append(float(row[col]))
            except (KeyError, TypeError, ValueError):
                continue
    return prices


def _print_result(name: str, res: dict) -> None:
    print(f"\n=== {name} ===")
    if res.get("error"):
        print("  error:", res["error"])
        return
    print(f"  trades:     {res['trades']}  recenters: {res['recenters']}")
    print(f"  equity:     ${res['final_equity']:,.2f}  (cash ${res['final_cash']:,.2f})")
    print(f"  buy&hold:   ${res['buy_hold_equity']:,.2f}")
    print(f"  vs B&H:     {res['vs_buy_hold_pct']:+.2f}%")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--scenario", choices=("all", "ranging", "uptrend", "crash"), default="all")
    p.add_argument("--ohlcv", default="", help="Optional CSV with close column")
    p.add_argument("--col", default="close")
    p.add_argument("--atr-pct", type=float, default=3.0)
    p.add_argument("--spacing-mult", type=float, default=0.8)
    p.add_argument("--cash", type=float, default=10_000.0)
    p.add_argument("--base-buy", type=float, default=500.0)
    p.add_argument(
        "--limits",
        action="store_true",
        help="Phase C: shadow limit-order book instead of market-touch slices",
    )
    args = p.parse_args()

    common = dict(
        atr_pct=args.atr_pct,
        spacing_atr_mult=args.spacing_mult,
        initial_cash=args.cash,
        base_buy_usdt=args.base_buy,
    )
    sim_fn = simulate_limit_grid_path if args.limits else simulate_plan_path

    scenarios: list[tuple[str, list[float]]] = []
    if args.ohlcv:
        px = _load_csv(args.ohlcv, args.col)
        if len(px) < 10:
            print(f"ERROR: need ≥10 prices from {args.ohlcv}, got {len(px)}", file=sys.stderr)
            return 1
        scenarios.append((f"ohlcv:{args.ohlcv}", px))
    elif args.scenario == "ranging":
        scenarios.append(("ranging", _series_ranging()))
    elif args.scenario == "uptrend":
        scenarios.append(("uptrend", _series_uptrend()))
    elif args.scenario == "crash":
        scenarios.append(("crash", _series_crash()))
    else:
        scenarios = [
            ("ranging", _series_ranging()),
            ("uptrend", _series_uptrend()),
            ("crash", _series_crash()),
        ]

    mode_label = "limit_shadow (Phase C)" if args.limits else "market slices (Phase A/B)"
    print(f"Grid plan backtest — {mode_label} — local only")
    print("(spacing: stable=0.55 · volatile=1.15 · meme=1.25 via same series)\n")
    for name, prices in scenarios:
        res = sim_fn(prices, **common)
        _print_result(name, res)
        if name == "ranging" and not args.limits:
            from strategies.grid_plan import spacing_atr_mult_for_coin

            for tier, mult in (
                ("stable", spacing_atr_mult_for_coin(volatility_tier="stable")),
                ("volatile", spacing_atr_mult_for_coin(volatility_tier="volatile")),
                ("meme", spacing_atr_mult_for_coin(coin_class="meme")),
            ):
                r2 = simulate_plan_path(
                    prices,
                    atr_pct=args.atr_pct,
                    spacing_atr_mult=mult,
                    initial_cash=args.cash,
                    base_buy_usdt=args.base_buy,
                )
                _print_result(f"ranging/{tier} spacing×{mult:.2f}", r2)

    print("\nDone. No deploy — review results before rollout.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
