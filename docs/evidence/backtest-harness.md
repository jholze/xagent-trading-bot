# Evidence: backtest harness (`backtest/harness`)

Research tooling only. No file under a money-path prefix and no money key in
`config.json` is changed. Reports: `docs/backtests/2026-11-01-harness-base.md`
and `-stress.md`.

## Tests

| Check | Before | After |
|---|---|---|
| Unit tests for a portfolio replay with the real stop/DCA code | none | 20 in `tests/unit/test_backtest_harness.py` |
| Property tests (hypothesis) | none, library not installed | 8 in `tests/unit/test_backtest_harness_properties.py`, `hypothesis` in requirements |
| DSR parity with `tools/entry_filter_replay.py` | n/a | equal to 1e-9 on 60 random series, N = 1, 12, 31 |
| PBO (CSCV) / block bootstrap | not in repo outside hermes fold deltas | `backtest/harness/stats.py`, tested |
| Bugs found by the property tests while writing | n/a | 2 fixed (moment underflow in DSR; last PnL bucket past the horizon) |

## Calibration on the real tape

| Item | Tape / export | Harness |
|---|---|---|
| Open lots, positions export vs tape (excl. `decisions`) | 125 in export | 125 matched, 13 tape-only |
| Hard stops Oct 3 - Oct 30 (`before` code 0319ceac) | 15 partial, 4 full | 19 partial, 4 full |
| Public candles | 283 symbols | 282 fetched; VANRY/USDT not on Gate (skipped, counted) |

## Scenario results (base costs; stress costs give the same verdicts)

| Scenario | Before (baseline) | After (candidate) | Windows | DSR | Verdict |
|---|---|---|---|---|---|
| (a) #733 + #738 on the October book | -69,033 USDT (-12.37 %) | -64,912 USDT (-11.63 %) | 2/3 | 0.21 | OBSERVE (bug fix, informational) |
| (b) vol sizing 0.05 | -0.80 % on invested | -0.31 % | 3/3 | 0.19 | OBSERVE |
| (c) SMA50 + distance | -0.80 % | +0.75 % | 3/3 | 0.46 | OBSERVE |
| (d) PR #740 denylist | -0.80 % | -0.79 % | 1/3 | 0.48 | KILL |
