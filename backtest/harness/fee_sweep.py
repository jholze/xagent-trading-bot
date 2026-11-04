"""Break-even fee sensitivity for one scenario's candidates (paper research).

``python -m backtest.harness.fee_sweep --data ... --candles ... --scenario p``

Re-runs the same candidates of a scenario with the taker fee per side swept
over a grid (slippage stays at the chosen cost tier) and prints the full-period
PnL of each candidate minus the baseline. Re-runs of the same candidates add
no trials to the register (backtest/harness/trials.py).
"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

FEES = (0.0, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 1.5, 2.0)


def sweep(ctx, variants, windows, tape_end, fees=FEES) -> dict:
    from .metrics import summarize

    eng = ctx.engine
    original = eng.costs
    start, end = windows[0][1], windows[-1][2]
    out: dict = {}
    try:
        for fee in fees:
            eng.costs = replace(original, fee_pct=fee)
            row = {}
            for pol, _desc in variants:
                m = summarize(eng.run(pol, ctx.episodes, start, end, carry_in=False, horizon_end=tape_end))
                row[pol.name] = m.pnl_usdt
            out[fee] = {k: v - row["baseline"] for k, v in row.items() if k != "baseline"}
    finally:
        eng.costs = original
    return out


def break_even(points: dict, name: str) -> float | None:
    """Lowest fee in the grid where the candidate's excess PnL turns <= 0 (linear interpolation)."""
    fees = sorted(points)
    prev = None
    for f in fees:
        v = points[f][name]
        if v <= 0:
            if prev is None:
                return f
            pf, pv = prev
            return pf + (f - pf) * pv / (pv - v)
        prev = (f, v)
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m backtest.harness.fee_sweep")
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--candles", type=Path, required=True)
    ap.add_argument("--scenario", default="p", choices=("p",))
    ap.add_argument("--cost-tier", default="base")
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args(argv)
    from . import scenarios as sc

    ctx = sc.build_context(args.data, args.candles, cost_tier=args.cost_tier)
    variants = sc._profit_variants()
    res = sweep(ctx, variants, sc.ENTRY_WINDOWS, sc.TAPE_END)
    names = [p.name for p, _ in variants if p.name != "baseline"]
    print("fee/side " + " ".join(f"{n:>18s}" for n in names))
    for fee, row in res.items():
        print(f"{fee:8.2f} " + " ".join(f"{row[n]:18.0f}" for n in names))
    be = {n: break_even(res, n) for n in names}
    print("break-even fee/side:", json.dumps(be))
    if args.json:
        args.json.write_text(json.dumps({"cost_tier": args.cost_tier, "excess_pnl_usdt": {str(k): v for k, v in res.items()}, "break_even_fee_pct": be}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
