"""``python -m backtest.harness`` - run scenarios and write docs/backtests/<stem>.md.

Example::

    python -m backtest.harness --data /workspace/trading-learning/railway_export \\
        --candles /workspace/bt-data/candles --fetch --scenarios a,b,c,d

``--fetch`` fills the candle cache from Gate's public OHLCV (no keys).
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from .benchmark import DEFAULT_BENCHMARK_SYMBOLS, BenchmarkConfig
from .policy import REPO_ROOT

UTC = timezone.utc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m backtest.harness", description=__doc__.splitlines()[0])
    ap.add_argument("--data", type=Path, required=True, help="dir with orders_v2_filled_alltime.jsonl, positions_current.jsonl, tenant_configs.jsonl")
    ap.add_argument("--candles", type=Path, required=True, help="candle cache dir (<tf>/<BASE>_<QUOTE>.json)")
    ap.add_argument("--scenarios", default="a,b,c,d")
    ap.add_argument("--cost-tier", default="base", choices=("base", "stress", "hard_stress"))
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "docs" / "backtests")
    ap.add_argument("--stem", default=None)
    ap.add_argument("--fetch", action="store_true", help="fetch missing public OHLCV first")
    ap.add_argument("--benchmark-symbols", default=",".join(DEFAULT_BENCHMARK_SYMBOLS),
                    help="comma-separated fixed Buy & Hold basket, equal weight (empty = none)")
    ap.add_argument("--no-traded-basket", action="store_true", help="skip the equal-weight basket of traded symbols")
    ap.add_argument("--benchmark-capital", type=float, default=None, help="capital base in USDT (default: peak deployed)")
    ap.add_argument("--no-benchmark", action="store_true", help="no Buy & Hold section")
    args = ap.parse_args(argv)

    from .scenarios import SCENARIOS, build_context

    started = datetime.now(UTC)
    bench_syms = tuple(x.strip() for x in args.benchmark_symbols.split(",") if x.strip())
    if args.fetch:
        from .data import CandleCache, fetch_public_ohlcv, load_book, load_fills

        fills = load_fills(args.data / "orders_v2_filled_alltime.jsonl")
        book = load_book(args.data / "positions_current.jsonl")
        syms = sorted({f.symbol for f in fills} | {k[1] for k in book} | set(bench_syms))
        cache = CandleCache(args.candles)
        fetch_public_ohlcv(cache, syms, "1d", datetime(2026, 3, 1, tzinfo=UTC), datetime.now(UTC))
        fetch_public_ohlcv(cache, syms, "1h", datetime(2026, 7, 20, tzinfo=UTC), datetime.now(UTC))

    ctx = build_context(args.data, args.candles, cost_tier=args.cost_tier)
    if not args.no_benchmark:
        ctx.benchmark = BenchmarkConfig(
            symbols=bench_syms, include_traded_basket=not args.no_traded_basket, capital=args.benchmark_capital
        )
    keys = [k.strip() for k in args.scenarios.split(",") if k.strip()]
    unknown = [k for k in keys if k not in SCENARIOS]
    if unknown:
        ap.error(f"unknown scenarios {unknown}")
    results = []
    for k in keys:
        print(f"scenario {k} ...", file=sys.stderr, flush=True)
        results.append(SCENARIOS[k](ctx))
    from .report import write

    stem = args.stem or f"{started:%Y-%m-%d}-harness-{args.cost_tier}"
    md, js = write(results, ctx.sources, started, args.out, stem)
    for r in results:
        print(f"({r.key}) {r.verdict.label}: {r.verdict.reason}")
    print(f"wrote {md} and {js}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
