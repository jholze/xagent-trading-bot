# Tenant rule switches (T, G, V, E, limits) – Requirements

Version: 1.0 · Date: 2026-11-02 · Status: frozen · Owner: Jens Holze

Rev 0. Frozen before the trading-code change in the same pull request.

## Why

The fresh-start backtest (`/workspace/umbau/neustart_bt`, plan "Neustart henry + default") found one package that passed the gate for henry: T + G + V + E (DSR 0.53, GO; default: OBSERVE). The package was assembled after the single-rule results were known, so it is in-sample. This spec only adds the switches. Every switch is **off** by default. Turning one on for a tenant is a config change at the fresh start, not part of this revision.

## Behavior

A new optional tenant config block `rule_switches` (top level, not under `risk`). A tenant without the block, or with `enabled: false`, behaves exactly as today.

```json
"rule_switches": {
  "trend_filter":  {"enabled": false, "sma_days": 50, "max_extension_pct": 20, "max_pump_24h_pct": 20},
  "no_chase":      {"enabled": false, "max_above_48h_low_pct": 15, "lookback_hours": 48, "min_bars": 36},
  "vol_sizing":    {"enabled": false, "target_daily_vol": 0.05},
  "age_loss_exit": {"enabled": false, "min_age_days": 14, "loss_pct": 15, "include_locked": false},
  "limits":        {"enabled": false, "max_usdt_per_order": null, "max_open_positions": null,
                    "max_position_pct": null, "max_lots_per_coin": null, "drawdown_brake_pct": null}
}
```

Definitions (same formulas as the harness: `tools/entry_filter_replay.py` `_sma_above`, `_pump_24h`, `_rv_daily`; `backtest/harness/scenarios.py` `_pump48_ok`, `_vol_multiplier`; `backtest/harness/engine.py` time stop):

- **New entry**: a BUY with no open long lot on the symbol (after the existing timeframe hop) and not a DCA add.
- **Closed bar**: open time + bar length ≤ decision time. The still-forming bar is never an input.
- **T trend_filter** (new entries, not manual): reject when the last closed daily close is not above the mean of the last 50 consecutive closed daily closes, or is more than 20 % above it, or the last closed 1h close is more than 20 % above the close 24 hourly bars earlier. Gapped or short data keeps the entry (harness rule).
- **G no_chase** (new entries, not manual): reject when the order price is more than 15 % above the lowest low of the closed 1h bars opened in the 48 h before the decision. Fewer than 36 bars keeps the entry.
- **V vol_sizing** (all non-manual buys, entries and adds): size × min(1, 0.05 / rv), rv = stdev of the last 24 closed hourly returns × √24. Missing data → 1.0. Applied after the per-ticket cap, before the concentration and cash caps.
- **E age_loss_exit**: each cycle, when the cycle did not already trade the symbol, an open long lot with age ≥ 14 days (from `entry_at`) and price ≤ average entry × 0.85 is sold in full with source and exit source `age_loss_exit`. A locked lot is skipped unless `include_locked` is true; then the risk manager lets only this source past the lock and only when the condition still holds.
- **limits**: `max_usdt_per_order` caps every buy (tighter of it and `max_usdt_per_trade`); `max_position_pct` caps the coin's share of equity (tighter of it and `max_position_percent`); `max_open_positions` and `max_lots_per_coin` reject new entries at the cap; `drawdown_brake_pct` rejects new non-manual entries while equity is that many percent below its peak (unknown equity also rejects new entries). DCA adds and all sells are not affected by max open, lots per coin, or the brake.

No rule in this spec is evaluated for a SELL. No stop, trail, hard stop, or manual sell can be rejected by it.

## Acceptance criteria

AC1. With no `rule_switches` block, `rule_config(..., rule)["enabled"]` is false for every rule, `limits_config` is all `None`, and a buy is approved with the same size as without this change.

AC2. With `trend_filter` on, a new entry below SMA50, more than 20 % above SMA50, or more than 20 % up in 24 h is rejected (`trend_filter_below_sma`, `trend_filter_extended`, `trend_filter_pump_24h`); a passing entry and an entry with missing data are approved. The same order with the switch off is approved.

AC3. With `no_chase` on, a new entry more than 15 % above the 48 h low is rejected (`no_chase_48h`); within 15 % or with fewer than 36 bars it is approved. Off: approved.

AC4. With `vol_sizing` on, the buy size is multiplied by min(1, 0.05 / rv); rv below 0.05 or missing leaves the size unchanged. Off: unchanged.

AC5. Each limit works alone: order cap, max open positions, max % per coin, max lots per coin, drawdown brake (including unknown equity). DCA adds pass max open, lots per coin and the brake.

AC6. Exits are never blocked: with every switch on and extreme market data, a SELL (stop and normal exit) is approved and the rule-switch gate is not called.

AC7. No lookahead: the still-forming 1h/1d bar and any bar closing after the decision time do not change T, G or V.

AC8. Parity: `sma_extension`, `pump_24h_pct`, `realized_daily_vol` and `pump_above_low_pct` equal the harness functions on the same candles.

AC9. Per-tenant isolation: one tenant's switches do not change another tenant's decision.

AC10. E sells an old, deep-loss lot in full with source `age_loss_exit`; not old enough, not deep enough, or switch off → no order. A locked lot is skipped unless `include_locked`; with `include_locked` only `age_loss_exit` passes the lock, other auto sources stay blocked.

AC11. Manual buys skip T, G, V and the brake.

## Acceptance tests

All in `tests/unit/test_tenant_rule_switches.py`.

| Criterion | Test |
| --- | --- |
| AC1 | `test_ac1_defaults_off`, `test_ac1_buy_unchanged_without_block` |
| AC2 | `test_ac2_trend_filter_*` |
| AC3 | `test_ac3_no_chase_*` |
| AC4 | `test_ac4_vol_sizing_*` |
| AC5 | `test_ac5_limit_*` |
| AC6 | `test_ac6_sells_never_blocked` |
| AC7 | `test_ac7_no_lookahead_*` |
| AC8 | `test_ac8_parity_with_harness` |
| AC9 | `test_ac9_tenant_isolation` |
| AC10 | `test_ac10_age_loss_exit_*` |
| AC11 | `test_ac11_manual_buy_skips_entry_rules` |

## Metric

Unit tests before/after; backtest numbers from `neustart_bt` (henry T+G+V+E +7,328 USDT vs. −13,691 baseline, DSR 0.53) are the expected effect, not a claim this code reproduces them. Known gap: the harness does not simulate max open positions or one lot per coin.
