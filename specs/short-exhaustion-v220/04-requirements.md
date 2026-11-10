# D-EXH – Requirements

Version: 1.2 · Date: 2026-11-10 · Status: **frozen 2026-11-10 by Kay (owner-delegated), scope raw-feature logging only; thresholds/lookback/divergence/grace remain open until measured**

Local draft id: D-EXH. Not a GitHub issue. Related existing issues, not closed and not re-scoped by this draft: #614 (old climax-fade), #694 (parked, not touched), #600, #628, #582, #769.

Hindsight: `RECALL_FAILED`. The bank `coding-agent::trading_bot` is not reachable from this cloud VM (Mac loopback only). Git and this file win over memory.

Scanner schema: `jholze/trading-analyst` pull 1 was not readable from this environment (GitHub 404). Head `011b49ff2bd7c9ff2fb7830a55a748710fe0dad3` was the sprint's read, not a pin. This revision does not claim that commit's runtime schema. The measurement input below is a local fixture contract.

## Rev 0 (archived 2026-11-10, sprint draft — not implemented)

The sprint draft asked for a frozen exhaustion recipe: extreme rise, positive extreme funding against a point-in-time baseline, high RSI, and bearish volume divergence, plus a freshness grace on the 4h cadence. It also asked for an offline replay against old Climax, an unconditional top-gainer fade, and no-trade.

That recipe was not implemented. Marcus marked the thresholds, the lookback, and the divergence definition UNCLEAR, and marked grace undefined. Rev 0 stays here so the wording is not rewritten in place. Rev 1 replaces it.

## Rev 1 (2026-11-10, Marcus correction — archived as the first measurement text)

Do not treat the numbers in old climax-fade, or any fixture number, as the exhaustion recipe. Rev 2 freezes the scope and replaces the holdout exclusion and the `execution.mode` names below. The open-definition table stays open.

### Open definitions

| Id | Item | State | This revision |
| --- | --- | --- | --- |
| Q-threshold | Rise cutoff, funding "extreme" addend, RSI cutoff, volume-ratio cutoff | UNCLEAR | Not stored and not applied |
| Q-lookback | Window for a rise, a funding baseline, or a divergence swing | UNCLEAR | Not chosen. Supplied point-in-time history is logged in full |
| Q-divergence | Bearish volume divergence, and the claim that a volume surge is not divergence | UNCLEAR | No divergence flag. Consecutive volume ratios and raw volumes are inputs only |
| Q-grace | How long after a closed 4h bar a row is still fresh | undefined | Not applied. Age in seconds is a raw field when `now` is supplied |

A later revision that sets any of these is Rev 2, dated, and does not edit this section in place.

### What is measured

For each scanner row that the caller passes in, log raw features and nothing else:

- Funding: every supplied settlement at or before `as_of`, with rate, interval (only if the row carries one), provenance, quote, and net cost (only if the row carries one). No 8h default. No median. No sign test. A missing rate stays unknown, not 0.
- RSI: Wilder RSI as implemented by `talib.RSI(..., timeperiod=14)`, the period already used in `MarketService._bars_to_dataframe`. The period is the indicator's period, not an exhaustion cutoff. There is no minimum. `indicators_from_df` is not used, because an empty frame there returns RSI 45.
- Volume: each closed bar's volume and close, and the ratio of each bar's volume to the previous closed bar's volume. No 4/20 climax multiple and no pass/fail.

`strategies.climax_fade.vol_mult_4_20` and `bar_return` stay the old climax recipe. They are not the exhaustion definition while Q-lookback and Q-threshold are open.

### Cadence versus grace

The 4h length is the bar size used to decide whether a supplied bar is closed at `as_of` (`close = open + 4h`, and `close <= as_of`). That is not a freshness grace. A row whose `as_of` is 30 minutes after the bar close and a row whose `as_of` is 48 hours after it are both logged. Q-grace stays undefined.

### What is not a trade decision

`not_measurable` means the feature numbers were not computed. It is not an order reject and not a short signal.

| Condition | Features | Why |
| --- | --- | --- |
| Malformed identity, bad JSON, or schema/revision mismatch | null | Unknown input, not a fabricated feature |
| `source_commit` missing or not 40 hex characters | null | Commit identity unknown. No scanner SHA is assumed |
| `as_of` after the caller-supplied `now` | null | Would be lookahead against the measurement clock |
| `as_of` at or after 2026-11-06 00:00 Europe/Berlin | null | Active hypothesis-protocol holdout is not read |
| Flag missing/false, mode live/mainnet/real, `shorts.allow_live` true, or mode not exactly `paper` or `futures_testnet` | production entry returns no rows | New path default off. No trading effect |

Future bars and future settlements are left out of the logged series. They are counted, not turned into a signal.

Point-in-time rule: a bar is in the series only when its close is at or before `as_of` and strictly before the holdout instant. A settlement is in the history only on the same clock rule. No wall clock is read inside the module.

### Flag

`shorts.exhaustion_v220.enabled` is on only when the boolean `true` is present on the config dict passed into the call. Missing, false, and any non-boolean stay off. `shorts.climax_fade.enabled` is not read. `config.json` is not modified. `#694` is not read and not revived.

When the flag is on and the mode is `paper` or `futures_testnet`, the result is a feature log. `order_intents` is empty and `venue_calls` is 0 in every mode.

### Trace

The log row uses the existing `#628` key tuple `strategies.buy_decision_tape.ROW_KEYS`. `filter_codes` is empty. Outcome text is `observed` or `not_measurable`, never `accepted` or `rejected`. The row is returned to the caller. This package does not append a file, does not call `append_jsonl`, and does not open a second ledger. A fill id may be referenced. The snapshot body stays with `#600` / package F. Keys outside `_ENTRY_SNAPSHOT_KEYS` are named as rejected keys and are not copied.

Tokenized-equity fields (`instrument_kind`, `listed_perp`, `calendar_known`, `depth_known`) are copied when they are booleans or, for kind, a string. Missing stays null. This revision does not deny the row and does not ban a symbol. The old deny sentence in the sprint draft waits on the owner freeze.

### Trial and the historical NO-GO

Trial id `D-EXH-R1-RAW` is recorded on the report as planned and not run. It is not inserted into `backtest/harness/trials.py` (`#769`): no scenario ran, and N must not grow. The dataset SHA-256 is taken from the caller inputs before feature values are computed. The cost protocol quotes `tools.hypothesis_protocol` (taker 0.2% per side and the class slippage table) and sets `applied_to_pnl` false. Spread and net funding cost stay unknown. Net expectancy, profit factor, drawdown, MFE, and MAE are null, not 0.

The 2026-11-05 Climax 1x summary stays a citation: profit factor 0.9160, −3500.42 USDT, n=1016, spot-proxy, verdict NO-GO, `recomputed: false`. It is not the result of this trial and it is not evidence that a new recipe has an edge. Baselines (old Climax, unconditional top-gainer fade, no-trade) are `not_run`.

### Acceptance criteria (Rev 1)

AC-EXH-1. A well-formed row is logged once. A repeated key, including when the duplicate arrives out of time order, does not create a second row. Malformed JSON, a schema mismatch, a future `as_of`, and a holdout `as_of` log `not_measurable` with null features. Age is logged and no grace drops an older row.

AC-EXH-2. Funding history, RSI period 14, and consecutive volume ratios are logged as raw values. A high RSI and a low RSI are both `observed` with `decision` null. No threshold, lookback cutoff, or divergence flag exists. Missing funding or a short close series is null, not 0. A non-decimal close does not become an RSI. `climax_fade` helpers are not the feature definition.

AC-EXH-3. Missing or non-true flag, live/mainnet/real mode, `allow_live: true`, and any other mode return no observations, no log lines, and no order intent. `climax_fade.enabled: true` does not turn the path on. An enabled paper or futures-testnet call still returns zero order intents and zero venue calls.

AC-EXH-4. The report status is `insufficient_data`. Profit factor and the other result fields are null. The historical NO-GO block matches the citation and is not recomputed. The dataset hash is stable and is taken before features. A settlement after `as_of` is absent from the funding history. Holdout rows are not measured. Costs are quoted and not applied. There is no profitability claim.

AC-EXH-5. Each logged row can be projected onto `ROW_KEYS` with empty `filter_codes` and without writing a file. An entry-snapshot reference is an id. A snapshot key outside the `#600` tuple is not stored. Two symbols, one with unknown tokenized-equity fields, are both logged. `#694` is not referenced by this package.

### Acceptance tests

| Criterion | Test |
| --- | --- |
| AC-EXH-1 | `tests/unit/test_short_signal_feed.py::test_ac_exh_1_source_freshness_dedup` |
| AC-EXH-2 | `tests/unit/test_short_signal_feed.py::test_ac_exh_2_raw_features_no_threshold` |
| AC-EXH-3 | `tests/unit/test_short_signal_feed.py::test_ac_exh_3_default_off_and_live_denied` |
| AC-EXH-4 | `tests/unit/test_short_signal_feed.py::test_ac_exh_4_replay_costs_no_lookahead` |
| AC-EXH-5 | `tests/unit/test_short_signal_feed.py::test_ac_exh_5_trace_reuses_existing_storage` |

The sprint name `test_ac_exh_2_four_filters_and_missing_inputs` is not used. A four-filter test would freeze the unclear thresholds.

### Metric

Before: the package does not exist. Importing `services.short_signal_feed` fails. Climax 1x remains NO-GO (PF 0.9160, −3500.42 USDT, n=1016, spot-proxy), not recomputed here.

After: a sandbox fixture with the flag on returns one feature log per distinct row, raw RSI/funding/volume, null decisions, and zero order intents. The same fixture with the flag off or with only `climax_fade.enabled` returns no log and no order. The trial report stays `insufficient_data`.

### Non-goals

- Emitting a buy, a short, a cover, or any other order intent.
- Wiring `services.trading_service`, `strategies.decision_engine`, the execution adapter, portfolio, or `config.json`.
- Editing `strategies/climax_fade.py` or enabling `#694`.
- Choosing grace, thresholds, lookback, or a divergence definition.
- A backtest, a holdout read, or a profitability claim.
- A second fill ledger or a shadow order book.

## Rev 2 (2026-11-10, Kay freeze — binding)

Status: frozen 2026-11-10 by Kay (owner-delegated), scope raw-feature logging only; thresholds/lookback/divergence/grace remain open until measured.

Kay has owner-delegated freeze authority for this paper/testnet scope (2026-11-10 18:40 Europe/Berlin). This revision does not choose a threshold, a lookback, a divergence definition, or a grace. It does not add an order.

Changes from Rev 1:

- A bar is in the series only when `close = open + 4h` and `close <= as_of`. A bar that is still open at `as_of` is excluded and counted in `volume.excluded_after_as_of`. Comparing the open time instead would admit it.
- A supplied `now` that does not parse makes every row `status: invalid` with reason `malformed_now`. Features stay null. That row is never `observed`. A missing `now` is different: age is unknown and the future-`as_of` check is not applied.
- `as_of` at or after 2026-11-06 00:00 Europe/Berlin is logged with `holdout: true`. Those rows are not blocked. They must not be used for threshold fitting. Profit factor and the other result fields stay null.
- `as_of` after a parsed `now` is still `not_measurable` / `future_as_of`, including when that timestamp is also inside the holdout.
- The sandbox gate reads `trading_mode` (`paper`, `demo`, `gate_testnet`) and `live.execution` (`shadow`, `testnet`). `trading_mode: live`, `live.execution: real`, `shorts.allow_live: true`, an unknown `live.execution`, or a legacy `execution.mode` of `live` / `mainnet` / `real` returns no rows. `trading_mode: live` with `execution.mode: paper` is denied.
- A gap between closed opens that is not exactly 4h marks that volume ratio `non_consecutive` and sets RSI null with reason `gap_in_closes`. `rsi.n_closes` is how many closes were passed to `talib.RSI` (0 when RSI is not computed). `n_closes_available` is how many decimal closes were present.
- A repeated `open_time` in the closed set fails closed for that bar set: `duplicate_open_time: true`, no bars, RSI null. Neither copy is chosen.
- Funding rates may be JSON numbers or decimal/scientific strings (`1e-4`). Anything else on a point-in-time settlement is reason `unparseable_funding`, not “no settlement” and not 0.
- A previous volume of 0 or a missing volume yields ratio null and reason `zero_or_missing_volume`. It is not divided.
- The `#600` snapshot key names are copied in this package. The ledger module is not imported.

### Acceptance tests added in Rev 2

| Check | Test |
| --- | --- |
| Bar still open at `as_of` is excluded and RSI is unchanged | `test_bar_closing_after_as_of_is_excluded` |
| Unparseable `now` | `test_malformed_now_fails_closed` |
| Holdout rows logged, not fitted | `test_holdout_rows_are_logged_and_not_fitted` |
| `trading_mode: live` plus `execution.mode: paper` | `test_ac_exh_3_default_off_and_live_denied` |
| Zero and missing volume | `test_zero_and_missing_volume_do_not_divide` |
| Gap | `test_gap_nulls_rsi_and_marks_ratio_non_consecutive` |
| Duplicate open | `test_duplicate_open_time_fails_closed_for_the_bar_set` |
| Numeric and `1e-4` funding; bad text | `test_funding_accepts_json_numbers_and_scientific_strings` |

## Changelog

| Version | Date | Change | Reason |
| --- | --- | --- | --- |
| Rev 0 | 2026-11-10 | Sprint draft of a four-filter exhaustion signal. Archived. Not implemented. | Local sprint text. Marcus had not frozen the recipe. |
| Rev 1 | 2026-11-10 | Measure and log raw features only. Mark Q-threshold, Q-lookback, Q-divergence, and Q-grace pending owner freeze. | Marcus: those definitions are UNCLEAR or undefined, so there is no filter and no signal. |
| Rev 2 | 2026-11-10 | Freeze the raw-log scope. Log holdout rows with `holdout: true`. Fail closed on a bad clock, a still-open bar, live mode keys, gaps, and duplicate opens. | Kay, owner-delegated, 2026-11-10 18:40 Europe/Berlin. Opus review on `1419843b`. No thresholds invented. |
