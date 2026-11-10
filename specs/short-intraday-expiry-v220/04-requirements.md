# Short intraday expiry – Requirements

Version: 0.1 · Date: 2026-11-10 · Status: draft · Owner: Jens Holze

Local draft id: T220-05 (not a GitHub issue). Revision: Draft Rev 0, 2026-11-10 Europe/Berlin.
Pending Marcus check. Hindsight: RECALL_FAILED.

This revision does not copy Owner-Freeze-Werte.md. Session clocks in tests are synthetic examples, including a 4h hold and a 20:00 Europe/Berlin stamp. They are not production cutoffs. The 4h figure is the existing `resolve_short_params` base, preserved when no overlay sets a cap. The bound "strictly under 24h" is the sprint safety rule, not a new 24h or 72h fallback.

#585 (closed) stays in force: naive ledger stamps are process-local, not labeled UTC. This path calls `ledger_datetime_utc`.

`risk/risk_manager.py`, execution, and portfolio files are not edited. No new scheduler, no weekend job, no new global cap or kill switch.

## AC1 — Flag off keeps today's cover

`shorts.sandbox_expiry_v220.enabled` is on only for boolean `true`. `climax_fade.enabled` does not turn it on. With the flag off, `evaluate_short_cover` keeps the current time-cap path, including an overlay of 30h.

Test: `tests/unit/test_v220_t220_05.py::test_ac1_flag_off_keeps_existing_cover`.

## AC2 — Hold is finite, positive, and under 24h

The sandbox resolver walks tier, then coin, then lot `time_cap_hours`. A missing stack uses 4h, the same base as `resolve_short_params` with no overlay. An explicit value that is missing-as-null, non-finite, not positive, or greater than or equal to 24h is invalid. Invalid does not become 24h, 72h, or the previous layer. `climax_fade.time_cap_hours` is not read. Lot `time_cap_hours` is applied here; `resolve_short_params` still ignores it.

Test: `test_ac2_hold_cap_bounds_and_overlays`.

## AC3 — Session due and budgets

When `session_required` is true, due is the earlier of the cap end and the session end. Exclusive session end is overdue at that instant. Inclusive session end is overdue on the following instant. Cap end stays overdue at `now >= cap_end`, matching the existing cover comparison. Execution and reconcile budgets must be explicit non-negative integers when a session is required. A missing budget is not zero. A new entry is open only when `now + budgets` and `planned_exit` are still inside the due. The 20:00 Europe/Berlin figure appears only as a synthetic session end in the test.

Test: `test_ac3_session_due_and_budget`.

## AC4 — #585, restart, stale, halt, timeout

Sandbox cover uses `ledger_datetime_utc`, so the #585 Berlin-naive 5h case still caps and the 3h case does not. After due, a restart with remaining quantity reports `overdue` and `flat` false. A stale price, a halt, or a timeout on an open lot reports overdue, not flat. Quantity zero is the only flat result. Unknown halt (`None`) on the exposure report is overdue, not flat. `evaluate_short_cover` does not treat an omitted halt as a halt, so a healthy flagged lot is not force-covered.

Test: `test_ac4_timestamps_restart_and_unpriced`.

## AC5 — Weekend, calendar, funding

No new weekend scheduler. On Saturday or Sunday in the calendar timezone, a new entry is denied unless `weekend_admission` is boolean true. A missing weekend key denies the weekend entry and does not deny a weekday. Stop cover still runs on Saturday with the flag on. Calendar `as_of` must be present and not in the future. Age equal to `max_age_seconds` passes; one second older is stale. A missing maintenance list is unknown, not "no maintenance". Inclusive and exclusive window edges are tested. A funding window accepts only `entry < settlement < planned_exit <= due`. A missing settlement stays missing when a caller passes an 8h interval. No funding amount is computed and the planned exit is not moved.

Test: `test_ac5_weekend_calendar_and_funding`.

## Parked

AC-03a/b/c numeric risk limits and the regime table are not part of this revision. AC-03d stock exclusion is not implemented here.

## Trace

| AC | Test |
| --- | --- |
| AC1 | `test_ac1_flag_off_keeps_existing_cover` |
| AC2 | `test_ac2_hold_cap_bounds_and_overlays` |
| AC3 | `test_ac3_session_due_and_budget` |
| AC4 | `test_ac4_timestamps_restart_and_unpriced` |
| AC5 | `test_ac5_weekend_calendar_and_funding` |

## Changelog

| Version | Date | AC | Change | Reason | Approved by |
| --- | --- | --- | --- | --- | --- |
| Draft Rev 0 | 2026-11-10 | AC1–AC5 | Default-off hold, session, calendar, and funding-window contract. No freeze-file numbers. | Sprint package C, T220-05 | Pending Marcus check. |
