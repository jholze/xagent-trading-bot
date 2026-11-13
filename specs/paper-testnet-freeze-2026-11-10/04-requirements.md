# Paper/testnet start values – Requirements

Version: 1.0 · Date: 2026-11-10 · Status: frozen for the named values · Owner: Jens Holze

Frozen at 2026-11-10 18:40 Europe/Berlin. Jens delegated the decision to Kay.
Scope is paper and futures testnet start values. Not live, not keys, not a
tenant change, and not a strategy approval. Existing in memory: RECALL_FAILED.

Items 3, 5, 6, 7, 9, 11, and 12 of the decision sheet are not implemented here.
Item 6 minutes and item 8 minute/amount limits stay unset on purpose.
Item 11 thresholds stay unset.

## AC1 Stop clearance

`shorts.testnet_futures.paper_start.liq_clearance_fraction` is `"0.05"` and
`liq_clearance_min_ticks` is `1`. For a short, the stop stays at least
`max(min_ticks * tick, fraction * (liquidation - entry))` away from the
estimated liquidation. Exact clearance passes. One tick closer is tightened
back to that distance, never left closer. A fraction below `0.05` or a tick
count below 1 is rejected. Missing entry, liquidation, or tick is
`no_exchange_data` and does not approve a new position. The existing
`shorts.liquidation_buffer` value `0.05` is unchanged. `short_cover.py` is
not edited.

Test: `tests/unit/test_paper_testnet_freeze_2026_11_10.py::test_item1_clearance_keeps_five_percent_and_one_tick_and_only_tightens`

## AC2 Host allowlist

`host_allowlist` is exactly `["https://api-testnet.gateapi.io:443"]`.
`legacy_host_fallback` is false. `https://fx-api-testnet.gateio.ws` is
rejected and `failover_hosts` is empty. A longer allowlist is
`allowlist_not_frozen`. Implicit port 443 matches the frozen origin.

Test: `test_item2_host_allowlist_is_exactly_the_testnet_origin_without_fx_fallback`

## AC4 Reconciliation tolerance

`reconciliation_tolerance_usdt` is `"0.01"`. Absolute gap `0.01` passes.
Gap `0.01` plus one `0.000001` USDT quantum fails. A tolerance above `0.01`
is `tolerance_widened` and does not pass. Unknown ledger or venue is not zero.

Test: `test_item4_tolerance_is_one_cent_and_does_not_widen`

## AC8 Emergency permit

`emergency_permit_default` and `emergency_auto_close` are false. The module
submits no close. A permit object is not an automatic close. Flipping
`emergency_auto_close` to true is `auto_close_forbidden`. No minute or USDT
limit is stored.

Test: `test_item8_emergency_permit_defaults_off_and_does_not_auto_close`

## AC10 ADL

`warning`, `elevated`, and `unknown` (including missing, blank, stale, and
any unrecognized token) block a new short. `keep_protection` stays true and
`auto_close` stays false. No clear ADL state is frozen, so unrecognized data
does not open a short.

Test: `test_item10_adl_warning_elevated_or_unknown_blocks_a_new_short_and_keeps_protection`

## Non-goals

No edit to `execution/gate_adapter.py`, `execution/recovery.py`,
`strategies/short_cover.py`, or `strategies/short_policy.py`. No order, no
key, `shorts.allow_live` stays false, `testnet_futures.enabled` stays false.

## Changelog

| Version | Date | AC | Change | Reason | Approved by |
| --- | --- | --- | --- | --- | --- |
| Rev 1 | 2026-11-10 | AC1, AC2, AC4, AC8, AC10 | Config defaults and tests | Owner delegated the freeze to Kay at 18:40 | Kay, by Jens' delegation. Not a live approval |
