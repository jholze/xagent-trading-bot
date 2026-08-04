# Path Stats Memory (Episode Backtests → Memory)

**Branch:** `feat/path-stats-memory`  
**Goal:** Multi-coin market-path episode stats in Memory so live exits can later bias per-coin policy for **max capture**.  
**Not the goal:** BE-lock alone; single-coin specialization.

## Principles

1. **Offline / cron** computes stats — never full backtest on the tick path.
2. **Live only reads** precomputed summaries (fail-open if missing/disabled).
3. **Ledger-safe:** only `memory_*` collections; never orders/positions writes.
4. **Rollback-first:** one kill-switch disables write + any future consumers.

## Episode definition (v1)

For each symbol / timeframe (default `1h`):

1. Lookback window `W=48` bars for local trough (`min low`).
2. From trough, track peak high.
3. **Arm event:** first bar where `peak/trough - 1` crosses a band threshold  
   `bands = [0.05, 0.08, 0.10, 0.12, 0.15, 0.20]`.
4. Forward horizon `F=24` bars (~1 day on 1h):
   - `max_giveback_from_arm_peak` = 1 − min(low)/peak_at_arm
   - `hit_trail_8` = min(low) ≤ peak_at_arm × 0.92
   - `reach_plus_5_ext` = max(high) ≥ peak_at_arm × 1.05
   - `end_gain_from_trough` = close_end/trough − 1

Aggregate per `(symbol, timeframe, band)` → medians / rates + `n` + `sample_quality` (`ok` if n≥5 else `thin`).

**Tier fallback (later consumer):** if coin thin, use tier aggregate (not in live path yet).

## Storage

- Collection: `memory_path_stats` only  
- `_id`: `{tenant}|{scope}|{symbol}|{timeframe}|{band}`  
- Kill: `MEMORY_PATH_STATS=0` **or** `memory.path_stats.enabled: false`

## Rollback

| Action | Effect |
|--------|--------|
| Set `MEMORY_PATH_STATS=0` | No refresh writes; getters return None/empty |
| Set config `enabled: false` | Same |
| Drop `memory_path_stats` | Optional wipe; ledger untouched |
| Revert git PR | No DE/trail wiring in this PR → zero behavior change when flag off |

## Soft bias (exit knobs)

When `path_stats` is enabled **and** `soft_bias.enabled` (default true):

- Live reads `memory_path_stats` for open positions only (`resolve_strategy_params`).
- Quality `ok` band near activation/prefer_band → small trail/arm deltas (±3 trail, ±2 arm).
- High median giveback / trail-hit → tighten; low giveback + high extension → loosen.
- Never flips `floor_at_entry` / `arm_on_peak`. Fail-open if missing/thin/error.
- Meta on params: `_path_stats_bias` for audit.

Refresh: `refresh_in_memory_cycle` (throttled, default 12h) in hermes memory cycle + CLI.

## CLI

```bash
# dry-run report only (no Mongo write)
python scripts/run_path_stats_refresh.py --dry-run --limit 30

# write to memory (requires flag enabled)
MEMORY_PATH_STATS=1 python scripts/run_path_stats_refresh.py --write
```

Universe (priority order, unique symbols, capped by `--limit`):

1. **Open positions**
2. **Symbols from recent filled trades** (ledger orders, newest first; scan up to `--trade-orders`, default 500)
3. Optional **watchlist** fill-up

Flags: `--no-trades`, `--no-watchlist`.

## Success (spike)

- [ ] Multi-coin report JSON with n / giveback / hit rates  
- [ ] Unit tests on synthetic OHLCV  
- [ ] Flag off → no writes  
- [ ] No imports from DE / exit_realtime execute path  
