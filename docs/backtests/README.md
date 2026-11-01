# Backtest harness (`backtest/harness`)

One command replays the bot's **own** exit and DCA code over the recorded order
tape on public hourly candles and writes a markdown + JSON report here:

```bash
python -m backtest.harness \
  --data /path/to/railway_export \        # orders_v2_filled_alltime.jsonl, positions_current.jsonl, tenant_configs.jsonl (read-only exports)
  --candles /path/to/candle-cache \       # <tf>/<BASE>_<QUOTE>.json
  --fetch \                               # fill the cache from Gate public OHLCV (ccxt, no keys)
  --scenarios a,b,c,d --cost-tier base    # or stress / hard_stress
```

Paper research only. The harness never imports an exchange client with keys,
never writes to Mongo or Redis, and never places orders.

## What it simulates

| Part | Source of truth in the run |
|---|---|
| Hard stop full / partial, DCA grace, widening | `strategies/dca.py` `evaluate_long_hard_stop` + `effective_stop_loss_thresholds`, loaded from the work tree or any git ref (`policy.load_module`) on a simulated clock |
| Partial size | `strategies/positions.py` `sell_fraction_for_signal` |
| Partial size guard (cap, min position, min notional) | same order as `RiskManager`, tenant `risk` limits |
| #738 escalation of a rejected hard partial | policy switch `escalate_rejected_hard_partial` |
| Locks | `positions.lock.locked_at` + `no_auto_sell` block stop sells from that time |
| Entries, adds, trail/TP/rotation/manual sells | recorded tape (`dca_mode="tape"`), or the real `should_dca` on bar indicators (`dca_mode="rule"`) |
| Fees and slippage | `tools/hypothesis_protocol.py` cost model (0.2 % taker per side, class slippage, stress tiers) |
| Stop fill price | min(bar open, max(stop level, bar low)) minus slippage |

Two modes:

* **book** - each window starts from the tape's own lot state at the window
  start (marked at the first open) and ends at the window end. Used for exit
  and DCA changes on the existing book.
* **entries** - lots entered inside a window run until the end of the tape.
  Used for entry filters and sizing.

## Statistics and verdict (release framework rule 4)

* Walk-forward: three disjoint windows; a candidate must beat the baseline on
  return on invested capital in at least 2 of 3.
* Deflated Sharpe (Bailey & Lopez de Prado 2014) on the daily excess return
  (candidate minus baseline), deflated for `N_TRIALS = 12` candidate variants
  per harness run; must be > 0.5. Same formula as `tools/entry_filter_replay.py`
  (tested for equality).
* PBO by CSCV (Bailey, Borwein, Lopez de Prado, Zhu) over all variants of a
  scenario, 8 blocks, 70 splits.
* Moving-block bootstrap (block 5 days, 2000 resamples, fixed seed) 95 % CI of
  the daily excess return; must exclude 0 for GO.
* Guardrails: share of open lots <= -20 %, max drawdown, capital in losing
  lots. Worse by more than 1 % relative = KILL.

GO = all of the above. OBSERVE = windows won but DSR or CI short. KILL =
fewer than 2 windows or a guardrail worse. Bug fixes (rule 5) get an
informational verdict.

## Why not an off-the-shelf framework

| Framework | License | Why not here |
|---|---|---|
| backtesting.py | AGPL-3.0 | single instrument, no multi-lot DCA, no partial-sell guard, no tenant state |
| vectorbt | Apache-2.0 + Commons Clause (PRO proprietary) | vectorized signals; path-dependent lot state (grace, widening, one partial per lot, locks) has to be re-coded |
| backtrader | GPL-3.0 | unmaintained since 2021, needs a strategy rewrite |
| freqtrade backtesting / hyperopt | GPL-3.0 | needs the bot rewritten as `IStrategy`, own wallet and trade model |
| nautilus_trader | LGPL-3.0 | high-fidelity event engine, but again a full port of strategy and risk code |
| jesse | MIT (live plugin paid) | own strategy API, same rewrite |

All of them test a **copy** of the strategy. Every recent loss came from the
bot's own exit/DCA/guard interaction, which a copy would not reproduce. The
harness therefore runs the real modules (and older versions straight from git)
and keeps the statistics small and auditable. Methods borrowed from the
literature, not the libraries: walk-forward / OOS, CSCV-PBO, deflated Sharpe,
block bootstrap, explicit cost tiers.

## Known limits

* Tape mode keeps recorded adds and non-stop sells; a policy that changes
  *when* the bot adds needs `dca_mode="rule"` (indicators RSI14, BB20, ATR14 on
  1h; funding and BTC underperformance are not available offline and score 0).
* Survivorship: only coins the bot traded are in the universe; delisted pairs
  without public candles are skipped and counted (`skipped_no_candles`).
* Tenant configs come from the latest export; values changed since then are
  not seen.
* The positions export is a single snapshot; the report prints how many open
  lots the tape and the export agree on.
