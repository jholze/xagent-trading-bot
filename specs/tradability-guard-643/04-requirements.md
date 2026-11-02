# Tradability guard by features (#643) – criteria eligibility – Requirements

Version: 1.0 · Date: 2026-11-02 · Status: frozen · Owner: Jens Holze

Rev 0 of the eligibility extension. Frozen before the trading-code change in the same pull request.
Extends issue #643 (R1–R9) inside the #641 guard (`services/venue_quality.py`). No second gate.
Builds on #755 (`rule_switches` block, `risk/rule_switches.py`).

## Why

The kauf_setup study (`/workspace/umbau/kauf_setup/kauf_setup_report.md`, sections 9–10) found that the
four coins on the former name lists (#491 `blacklist_bases`, closed PR #740) share measurable traits at the
buy: collapse versus the 90-day high (LAB, H), dead daily volume behind a one-day spike (AVAIL), and a pump
on a thin base (BLESS). A rule on those traits catches every coin with the same properties and needs no
names (#643 R1). Harness scenario (k) (#745) gives verdict OBSERVE (windows 3/3, DSR 0.11 at N = 55).

## Behavior

Four new rules in the tenant `rule_switches` block. Each has a `mode`:

| mode | effect |
| --- | --- |
| `off` (default) | rule not evaluated, no candle fetch |
| `shadow` | rule evaluated, a would-block is logged and written to the buy decision tape, the buy goes on |
| `enforce` | a block rejects the buy with the rule's code |

```json
"rule_switches": {
  "elig_liq7":       {"mode": "off", "min_median_quote_volume_usdt": 100000, "days": 7, "min_days": 3, "on_missing": "keep"},
  "elig_collapse90": {"mode": "off", "min_close_to_high": 0.20, "days": 90, "min_days": 20, "on_missing": "keep"},
  "elig_chase48":    {"mode": "off", "max_above_48h_low_pct": 15, "on_missing": "keep"},
  "elig_mcap":       {"mode": "off", "on_missing": "block"}
}
```

Definitions. A **closed bar** has open time + bar length ≤ decision time; the forming bar and any later
bar are never inputs. Quote volume of a daily bar = base volume × close.

- **E1 `elig_liq7`**: block when the median quote volume of the last 7 closed daily bars is below
  100,000 USDT. Fewer than 3 closed bars = missing. Complements L2 (24h ≥ 500k), which a one-day spike passes.
- **E2 `elig_collapse90`**: block when the last closed daily close is at most 20 % of the highest high of
  the last 90 closed daily bars (a drawdown of 80 % or more). Fewer than 20 bars = missing.
- **G15 `elig_chase48`**: block when the order price is more than 15 % above the lowest low of the closed
  1h bars opened in the 48 h before the decision (`services.shadow_rules.pump_pct_48h`, #747). Fewer than
  36 bars = missing.
- **M `elig_mcap`**: the #563 long market-cap floor (5M / 20M by tier,
  `strategies.short_policy.resolve_long_market_cap_min_usd`, `data.cmc_market_cap.resolve_market_cap_usd`)
  applied to **DCA adds on an open lot**. New entries and the #707 leftover-lot path keep the existing
  always-on #563 check unchanged; this rule only extends it. Unknown market cap blocks (#563 semantics).

Scope and seat:

- **S1 Every BUY** passes the rules in `RiskManager._buy_lock_decision`, after the final size and after the
  #641 L3 → L2 → L1 checks: new entries, DCA adds (`dca`, `dca_sniper*`, `deploy_boost`, `dca_recovery`),
  second timeframe, gainer and bot MCP buys, and the #707 path (a leftover lot below the venue minimum
  counts as a new entry and is checked).
- **S2 Fixed order** E1 → E2 → G15 → M, each with its own code. The first enforced block sets the decision
  code; all enforced and shadow codes are kept in the tape record.
- **S3 Never for exits.** No rule is evaluated for a SELL, stop, trail, hard stop, kill-switch flatten,
  manual sell or short cover.
- **S4 Manual.** The exact source `manual` is logged only (same exemption as #641). Bot MCP buys are checked.
- **S5 Missing data.** Per rule `on_missing`: `keep` (default for E1, E2, G15, same as harness scenario (k))
  or `block`. An exception inside the check is logged at WARNING and the buy continues.
- **S6 Mode parsing.** Missing key or `null` → `off`. `true` → `enforce`, `false` → `off`. Any other
  malformed value → `enforce` (#643 R4: a malformed value must not silently lift the guard).
- **S7 Tape (#628).** Every buy decision row gets an `eligibility` field (`null` when every rule is off):
  `decision_unix`, `enforced`, `shadow_would_block`, `modes`, `values` (`qv7_median_usdt`,
  `close_to_90d_high`, `above_48h_low_pct`, `market_cap_usd`) and per-check `verdict`, `value`,
  `threshold`. Rejections also carry `details.eligibility` and a message with value and threshold.
  The Mongo mirror `obs_buy_decision_tape` receives the same row.
- **S8 No names.** No coin symbol or base in non-test code; names appear only in test fixtures.

## Deviations from #643 (owner decision 2026-11-02)

- **R4 "No enable flag"**: replaced by the per-tenant `off | shadow | enforce` mode the owner asked for,
  default `off`. The thresholds still have numeric defaults in code.
- **R3 "Fail-closed"**: E1, E2 and G15 default to `on_missing: keep` so live behaviour matches the tested
  harness (40 of 1,043 lots had missing features). `on_missing: block` is available per tenant. M keeps
  #563 fail-closed.
- **R2 short opens**: not covered here (buys only, as requested). S1 spread and F1 trade frequency from
  #643 stay open; they would run after L1 and before E1.

## Intended tenant settings (not part of this revision)

henry and default, after the fresh start: `elig_liq7` enforce, `elig_collapse90` enforce,
`elig_chase48` shadow (most of the recall cost comes from G15; decide after the #749 holdout),
`elig_mcap` shadow. This revision changes no tenant config.

## Acceptance criteria

| # | Criterion | Test (`tests/unit/test_criteria_eligibility.py`) |
| --- | --- | --- |
| AC1 | Defaults off, repo config enables nothing, all-off fetches nothing and sizes as before | `test_defaults_all_off`, `test_shipped_and_tenant_configs_enable_nothing`, `test_all_off_no_fetch_no_change` |
| AC2 | Each rule: off = no effect, shadow = approved and logged, enforce = rejected with its code | `test_each_rule_off_shadow_enforce`, `test_each_rule_passes_healthy_coin`, `test_thresholds_at_boundary` |
| AC3 | Fixed order, all codes recorded | `test_codes_in_fixed_order_and_all_reported` |
| AC4 | Mode parsing incl. malformed → enforce | `test_rule_mode_parsing` |
| AC5 | Missing data keep / block; check error fails open | `test_missing_data_keeps_by_default_and_blocks_when_configured`, `test_check_error_fails_open_and_logs` |
| AC6 | DCA adds and the #707 path are checked; mcap floor covers DCA adds | `test_dca_adds_are_checked`, `test_leftover_dust_lot_707_is_checked_like_a_new_entry`, `test_mcap_floor_extends_to_dca_adds`, `test_mcap_rule_does_not_touch_new_entries` |
| AC7 | Sells, stops, exits never blocked or evaluated | `test_sells_stops_exits_never_blocked`, `test_check_ignores_non_buy` |
| AC8 | No lookahead (forming and future bars ignored) | `test_forming_daily_bar_ignored`, `test_future_bars_ignored`, `test_forming_hourly_low_ignored` |
| AC9 | Tape row carries reason codes and values for shadow and reject | `test_tape_records_shadow_values`, `test_tape_records_rejection_reason_and_values`, `test_tape_eligibility_null_when_off` |
| AC10 | The 4 former name-list coins are caught by features only | `test_former_denylist_coin_caught_by_data`, `test_former_denylist_under_intended_config`, `test_no_coin_names_in_live_code` |
| AC11 | Live rule = harness scenario (k) | `test_parity_with_harness_scenario_k`, `test_harness_n_trials_counts_scenario_k` |
| AC12 | Manual buy logged only | `test_manual_buy_is_logged_not_blocked` |

## Out of scope

Removing `gainer_universe.blacklist_bases` / `exclude_symbols` (separate PR after the shadow phase).
Enabling any mode in any tenant. S1 spread, F1 trade frequency, `st_tag`, listing age. Short opens.
