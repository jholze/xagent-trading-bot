# Net cash flow and partial-fill replay – Requirements

Version: Rev 1 · Date: 2026-11-10 · Status: **binding for the pure module in this phase**

Local draft id: T220-04. Not a GitHub issue. Existing issue: #659. Out of scope: #665 (TWR/capital), #676 (generic partial polling; B owns it), #68 (funding fetch/geo).

Hindsight: `RECALL_FAILED`. The bank `coding-agent::trading_bot` is not reachable from this cloud VM (Mac loopback only). Git and this file win over memory.

Package F still owns `services/portfolio_service.py`, `core/models.py`, and `strategies/positions.py`. This revision does not edit those files or `execution/`.

## Rev 0 (archived 2026-11-10, sprint draft — not edited in place)

The sprint asked for signed venue/testnet cash flow as a separate provenance from paper funding, with no exchange account query and no second ledger. A synthetic example, not a default, was: 1 unit short at 100, cover at 90, fees 0.10 and 0.09, funding receipt +0.02 gives 9.83; funding payment −0.02 gives 9.79. Unknown, non-USDT, or missing fee or settlement stays unknown with a reason, not an actual zero. Raw trade, funding, and ADL record ids are deduped across restart. Like-for-like comparison uses a supplied tolerance; a missing or unapproved tolerance blocks acceptance instead of widening it. Venue realised PnL that already includes fees or funding is not charged again. Paper funding stays labeled simulation. Testnet is not live PnL. A settlement just before, at, or after the window, including a late publication, needs an explicit account window. An indicative funding print is not a settlement. Absence of a print is `no_settlement_observed`.

#659, already specified, asks the existing replay to keep executed quantity on `partially_filled` and remainder-canceled spot rows, using the #655 cost basis, without double booking a duplicate or a later cumulative row.

Marcus on 2026-11-10 18:21 left release values pending. The owner freeze the same day at 18:40 set item 4: T = 0.01 USDT. Exact 0.01 passes. One money quantum over T fails. T is not widened when venue rounding cannot meet it.

## Rev 1 (2026-11-10, this phase)

The pure module is `core/net_cashflow.py`. `core/sim_ledger_replay.py` calls its row predicate and still applies the #655 basis itself. There is no second replay engine.

### Frozen value

| Id | Value | Rule |
| --- | --- | --- |
| T | 0.01 USDT | Only this value is an approved money tolerance. A missing tolerance, a float, or any other number, including a wider one, blocks. Config cannot replace it. |
| Money quantum | 0.00000001 USDT | The step used to prove T+quantum fails. It is not a second tolerance. |
| Quantity precision | not frozen | A quantity compare cannot pass. Money T is not reused as a coin tolerance. Pending Marcus check on the future quantity unit. |

### Flag

`accounting.net_cashflow_v220.enabled` is on only when the boolean `true` is present. Missing, false, and any non-boolean stay off. The flag gates `account`. It does not gate the #659 replay predicate: that is an existing bug fix, and hiding it behind the flag would leave the bug active. `shorts.climax_fade.enabled` is not read. `shorts.allow_live: true`, trading mode `live`, `live.execution: real`, and any mode outside `paper`, `demo`, `gate_testnet`, and `futures_testnet` deny the account path. `config.json` is not modified.

The flag key path is an implementation choice under the default-off rule. **Pending Marcus check** on the key name only. The off behavior is required.

### Accounting

`actual_net = price_pnl - fees + signed_funding + signed_adl` only when every required leg is actual USDT.

- Short price PnL is `qty * (entry - exit)`. Long price PnL is `qty * (exit - entry)`.
- Fees are non-negative costs. A missing, unknown, non-USDT, or float fee makes `actual_net` null. It is not skipped as zero.
- Venue funding is signed: positive is a receipt, negative is a payment. It counts only when its settlement instant is inside the caller window. Publication time is not the clock.
- Inclusive start and inclusive end are required booleans. A missing flag, a naive timestamp, or an inverted window is unknown.
- An indicative record is not a settlement. A record marked `no_settlement_observed` or `absent`, or no funding record at all, is not funding zero.
- Paper funding is reported separately with label `simulation`. It does not enter `actual_net`.
- The same `(kind, record_id)` is booked once. Two amounts on one id block. A missing id is not given a synthetic id.
- Incremental fills sum distinct trade ids. Cumulative snapshots of one order keep the latest `as_of` quantity, not the sum. Mixed or missing quantity semantics block. This does not poll the exchange and does not replace #676.
- An ADL or realised record whose `includes` overlap price, fees, or funding is kept as a venue figure and is not added to, or subtracted from, the leg sum.
- `live_pnl` is false and `venue_calls` is 0 on every result.

### Replay (#659)

A spot buy or sell counts when status is `filled`, or when status is `partially_filled`, `canceled`, or `cancelled` and `execution.amount` is greater than 0. The request amount and request quote are not used for those partial rows. Rows with filled quantity 0 or missing stay ignored and are not logged.

Rows that share an order identity (`exchange_order_id`, else `id`, else `order_id`, else `client_order_id`, plus side) are applied once. The later fill time wins. On a tie, the larger execution amount wins, then the later input. That is the current cumulative fill, not the sum.

Short and cover rows still count only when status is `filled`. Their price-only branch is unchanged.

Each taken partial or remainder-canceled spot row logs one INFO line per replay call with tenant, symbol, side, status, and filled quantity.

`is_executed_status` still means filled. The daily-loss reader is not changed in this revision (see the hook).

### Acceptance criteria

AC-659-1. A remainder-canceled buy of 40 coins and 40.08 USDT, with a larger request, reloads as amount 40 and average entry 40.08/40, and cash falls by 40.08.

AC-659-2. A `partially_filled` row and a later `filled` row with the same id, including a duplicate of the filled row, count once at the cumulative filled quantity.

AC-659-3. A remainder-canceled sell of 20 from a 100-coin lot leaves amount 80, the same average entry, and realized PnL on 20. The predicate accepts the row. `is_executed_status` does not.

AC-659-4. Canceled or partially filled rows with amount 0 or missing, including a positive request size, are ignored and not logged.

AC-659-5. A cost-stamped partial buy uses the stamped quote and the stamped amount.

AC-659-6. A filled short still opens. A partially filled short and a canceled cover do not change that lot. A filled buy with amount only on the request still uses that request amount.

AC-04-1. The synthetic receipt is 9.83 and the synthetic payment is 9.79. Both are examples. `live_pnl` is false and `venue_calls` is 0.

AC-04-2. Duplicated fee, funding, and ADL ids book once. Cumulative partial snapshots book the latest quantity once. Incremental trade ids sum once. Mixed semantics block.

AC-04-3. An unknown fee, a non-USDT fee, a float fee, an indicative funding print, and an explicit absence of settlement leave `actual_net` null. Paper funding is labeled simulation and is not the venue leg.

AC-04-4. Settlement at the inclusive bounds counts. One step outside does not. A late publication of an in-window settlement counts. A publication inside the window does not pull in an out-of-window settlement. Missing inclusive flags and a naive timestamp block.

AC-04-5. `|ledger-venue| = 0.01` passes. `|ledger-venue| = 0.01000001` fails. Tolerance 0.02, a float tolerance, a missing tolerance, and a config tolerance do not pass and do not widen T. A quantity compare does not pass.

AC-04-6. A venue figure that already includes price, fees, and funding is not added to the leg sum. A unlike include set blocks the compare. The flag off, a string "true", the old climax flag, live mode, `allow_live`, and a non-sandbox mode produce no actual net. `futures_testnet` with the flag on can account. The module imports no venue client. The replay cover line stays price PnL.

### Acceptance tests

| Criterion | Test |
| --- | --- |
| AC-659-1 | `tests/unit/test_issue_659_partial_replay.py::test_t1_remainder_canceled_buy_books_execution_not_request` |
| AC-659-2 | `tests/unit/test_issue_659_partial_replay.py::test_t2_same_order_counts_once_at_current_cumulative_fill` and `test_restart_duplicate_of_canceled_buy_books_once` |
| AC-659-3 | `tests/unit/test_issue_659_partial_replay.py::test_t3_remainder_canceled_sell_keeps_basis_and_filled_pnl` |
| AC-659-4 | `tests/unit/test_issue_659_partial_replay.py::test_t4_zero_or_missing_fill_is_ignored_and_not_logged` |
| AC-659-5 | `tests/unit/test_issue_659_partial_replay.py::test_t5_cost_stamped_partial_buy_uses_stamped_usdt` |
| AC-659-6 | `tests/unit/test_issue_659_partial_replay.py::test_t6_short_and_cover_partials_do_not_change_the_short_branch` and `test_filled_buy_still_falls_back_to_request_amount` and `test_partial_rows_log_once_per_taken_order` |
| AC-04-1 | `tests/unit/test_v220_t220_04.py::test_ac04_example_receipt_is_9_83_and_payment_is_9_79` |
| AC-04-2 | `tests/unit/test_v220_t220_04.py::test_ac04_restart_does_not_double_book_ids_or_cumulative_qty` and `test_ac04_incremental_fills_sum_once_and_mixed_semantics_block` |
| AC-04-3 | `tests/unit/test_v220_t220_04.py::test_ac04_unknown_fee_and_missing_settlement_are_not_actual_zero` and `test_ac04_paper_funding_stays_simulation_and_is_not_the_venue_leg` |
| AC-04-4 | `tests/unit/test_v220_t220_04.py::test_ac04_settlement_window_bounds_and_late_publication` |
| AC-04-5 | `tests/unit/test_v220_t220_04.py::test_ac04_tolerance_exact_t_passes_and_wider_t_is_rejected` and `test_ac04_quantity_precision_does_not_reuse_money_t` |
| AC-04-6 | `tests/unit/test_v220_t220_04.py::test_ac04_mapping_mismatch_does_not_subtract_fees_again` and `test_ac04_flag_missing_or_live_returns_no_actual_and_no_zero` and `test_ac04_module_imports_no_venue_client_and_replay_cover_stays_price_only` |

### Portfolio hook (not this revision)

Do not change cover behavior until F releases `services/portfolio_service.py`.

1. In `PortfolioService.execute_cover`, after `pnl = unrealized_pnl("short", qty, entry, price)` and beside `_cover_funding_usdt`, call `core.net_cashflow.account(config.raw, payload)` only when the flag is boolean true and `path_reason` is `open`.
2. Store that dict on the trade row as `venue_cashflow`. Do not assign `actual_net` into `pnl` when it is null, and do not coerce it to 0. Leave the existing paper `funding_usdt` / `funding_unknown` fields and the #373 warning as they are when the flag is off.
3. Fee legs: a missing `fee_usdt` is `state="unknown"`. Do not copy the current `reported_fee = 0.0` default into the venue payload. Estimated open fees stay unknown, not actual.
4. Quantity is the delta `execute_cover` already computed (`reported_qty - booked_qty`). Do not infer a cumulative fill again. #676 owns generic partial polling.
5. Window start is `entry_at`, window end is the cover time, both timezone-aware, with explicit inclusive flags. Do not extend the hold and do not add a funding fetch.
6. In `replay_simulated_ledger`, the cover line `pnl = cover_amt * (entry - price)` stays price-only until that same call is added.
7. Daily-loss reader: `OrderService._stats_filled_window` and `storage.order_ledger_v2.stats_from_filled_orders` should include sell rows for which `row_counts_for_replay` is true, using the pnl they already sum for filled sells. Do not change `is_executed_status` (`core/models.py` is F's file; that helper means filled). `query_day(filled_only=True)` will keep missing remainder-canceled sells until it grows the same predicate.

### Metric

Before: `core.net_cashflow` does not exist. `replay_simulated_ledger` skips every row whose status is not `filled`, so a remainder-canceled buy with a positive execution amount reloads as no lot.

After: the #659 tests above pass on `replay_simulated_ledger`, and the T220-04 tests pass on `account` with the flag on. The flag off returns `actual_net` null. No fixture test is a venue reconciliation or a profit claim.

### Non-goals

- Editing `services/portfolio_service.py`, `core/models.py`, `strategies/positions.py`, or `execution/`.
- A second ledger, a reset, or a backfill of stored pnl.
- An exchange, testnet, or authenticated call.
- Changing paper cover funding, #373 assertions, or #665.
- A profitability claim.
