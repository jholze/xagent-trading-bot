# Short testnet protection – Requirements

Version: 2.0 · Date: 2026-11-10 · Status: draft · Owner: Jens Holze

Local draft id: T220-02 (not a GitHub issue). Existing in memory: RECALL_FAILED.
#380 is the existing reduce-only stop ticket. This revision does not rewrite it.
Pending Marcus check: the deltas named below that are not already in #380.

## Rev 1 archive

Rev 1 full text was not in this worker's attachments
(`/workspace/umbau/v220_planning_2026-11-09/drafts/T220-02.md`). This file does
not invent that text. Safety carried forward from #380 and the 10 Nov sprint:
reduce-only protection, no naked flatten, uncertain create does not open
again, a stop failure is recorded rather than hidden.

## Flag

`shorts.testnet_futures.protection` must be literal `true`. Missing or any
other value is off. Off means `inhibits_entries=false`, zero venue calls, and
`cover_allowed` stays equal to the caller's separately authorized COVER.
`shorts.allow_live`, `shorts.climax_fade.enabled`, and
`shorts.testnet_futures.enabled` do not turn this flag on. Committed
`config.json` is unchanged.

The planner is not wired into `execution/gate_adapter.py`. It does not import
the unmerged #676 helper. Callers pass a venue-confirmed residual.

## AC1 One effective stop

A confirmed owned residual with no open reduce-only stop of that exact
quantity gets one planned stop: side buy, `reduce_only=true`, quantity equal
to the venue residual. One matching open stop plans nothing more. Two
matching stops plan nothing more, cancel nothing, and inhibit entries
(`duplicate`).

Test: `tests/unit/test_v220_t220_02.py::test_ac_t02_confirmed_residual_gets_exactly_one_reduce_only_stop`

## AC2 Uncertain create

`uncertain_open` plans zero opening orders even when the caller sets
`want_open`. Entries are inhibited. A non-synthetic client is never called.

Test: `test_ac_t02_uncertain_create_does_not_open_again`
Test: `test_ac_t02_real_shaped_client_is_not_called`

## AC3 Stop timeout, reject, cancel

A problem stop and no matching open stop reports `unprotected` when the venue
residual is confirmed, inhibits entries, plans no replacement stop, and plans
no flatten. An authorized COVER stays allowed.

Test: `test_ac_t02_stop_timeout_reject_and_cancel_report_unprotected`

Pending Marcus check: this plan does not immediately replace a rejected stop.
#380 records the failure and does not market-close; replacement is a later
decision.

## AC4 No unpermitted flatten

Default permit is absent. A flatten is submitted only when `allow` is literal
true and `run_id`, `tenant_id`, and `position_id` match the current run,
tenant, and residual. Quantity above the venue residual plans nothing
(`would_flip`). The only flatten side is buy with `reduce_only=true`. An
unconfirmed venue reports `unknown` and submits nothing.

Test: `test_ac_t02_flatten_needs_a_scoped_permit_and_cannot_flip`
Test: `test_ac_t02_unknown_venue_reports_unknown_and_does_not_flatten`

## AC5 Venue residual, orphans, COVER

A new stop uses the venue residual, not the local lot. A partial-cover or ADL
fixture therefore sizes to the venue quantity. An open stop whose quantity
does not match that residual is not canceled and is not joined by a second
stop (`stop_qty_mismatch`).

An orphan order or position with no owned lot inhibits entries, cancels
nothing, adopts nothing, and does not authorize COVER. An owned residual with
an extra unknown order is still protected, the unknown order is not canceled,
and an authorized COVER stays allowed.

Test: `test_ac_t02_partial_cover_and_adl_use_venue_residual_not_the_local_lot`
Test: `test_ac_t02_orphan_without_a_lot_blocks_entries_and_is_not_adopted`
Test: `test_ac_t02_missing_flag_submits_nothing_and_keeps_cover`

Pending Marcus check: mismatched open stops are left in place rather than
canceled. That avoids a blind cancel. It also means the "exactly one" outcome
is not forced by deleting the stale stop.

## Non-goals

No global kill switch. No strategy-threshold edit. No tenant overlay. No
runtime key. No real or testnet order. No edit to recovery or the adapter
while package A holds them. #412 stays closed NOT_PLANNED.

## Changelog

| Version | Date | AC | Change | Reason | Approved by |
| --- | --- | --- | --- | --- | --- |
| Rev 1 | 2026-11-09 | prior pack | Not attached. Not rewritten here. | Local planning pack | 09 Nov corrections are not approval of these deltas |
| Rev 2 | 2026-11-10 | AC1–AC5 | Default-off planner and fixture tests. Not wired. | Sprint package B | Pending Marcus check on the deltas named above |
