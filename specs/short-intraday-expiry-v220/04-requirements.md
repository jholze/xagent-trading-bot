# Short intraday expiry – Requirements

Version: 0.2 · Date: 2026-11-10 · Status: draft · Owner: Jens Holze

Local draft id: T220-05 (not a GitHub issue). Hindsight: RECALL_FAILED.

## Rev 1 — 2026-11-10 18:40 Europe/Berlin

Owner freeze item 3, delegated to Kay. Item 6 stays parameterized: missing execution or reconcile budgets deny with `buffer_unknown`. No minute value is invented.

Paper/testnet start schedule, only when `shorts.sandbox_expiry_v220.enabled` is boolean true:

- Cutoff 20:00:00 Europe/Berlin, Monday–Friday. A new short at or after 20:00:00 is denied. An explicit session end later than that cutoff is `cutoff_loosened`.
- Maximum hold is 4 hours. Explicit 0, 8, 24 and 72 hour overlays are invalid and are not replaced with 24 or 72. A tighter overlay (under 4h) still applies. The flag-off path is unchanged. With the flag on, only lots tagged `sandbox_path = v220_paper` use this schedule. Untagged lots keep the existing cover rules.
- Planned exit must be the same Europe/Berlin calendar day as the entry.
- No new short on Saturday or Sunday. `weekend_admission: true` does not loosen that. Stop and cover of an open short still run.
- This PR does not edit `config.json`. Numeric ceilings live in code. `shorts.testnet_futures.paper_start`, when #807 adds it, can only tighten those ceilings. If that key is absent, regime cells stay closed. `allow_live` stays false.

Rev 0 below is the pre-freeze draft. Its "<24h" ceiling is tightened to 4h for the sandbox path only.

## Rev 0 — 2026-11-10 Europe/Berlin

Pending Marcus check. Session clocks in the original draft were examples. The 4h figure is the existing `resolve_short_params` base.

#585 (closed) stays in force: naive ledger stamps are process-local, not labeled UTC. This path calls `ledger_datetime_utc`.

`risk/risk_manager.py`, execution, and portfolio files are not edited. No new scheduler, no weekend job, no new global cap or kill switch.

## AC1 — Flag off keeps today's cover

`shorts.sandbox_expiry_v220.enabled` is on only for boolean `true`. `climax_fade.enabled` does not turn it on. With the flag off, `evaluate_short_cover` keeps the current time-cap path, including an overlay of 30h.

Test: `tests/unit/test_v220_t220_05.py::test_ac1_flag_off_keeps_existing_cover`.

## AC2 — Hold is finite, positive, and at most 4h on the tagged path

Rev 0 said "under 24h". That ceiling is superseded. The sandbox cap is 4 hours.

The sandbox resolver walks tier, then coin, then lot `time_cap_hours`. A missing stack uses 4h, the same base as `resolve_short_params` with no overlay. An explicit value that is missing-as-null, non-finite, not positive, greater than 4h, or a plan past 20:00 Europe/Berlin is invalid. Invalid does not become 24h, 72h, or the previous layer. `climax_fade.time_cap_hours` is not read. Lot `time_cap_hours` is applied here; `resolve_short_params` still ignores it.

Test: `test_ac2_hold_cap_bounds_and_overlays`.

## AC3 — Session due and budgets

When `session_required` is true, due is the earlier of the cap end and the session end. Exclusive session end is overdue at that instant. Inclusive session end is overdue on the following instant. Cap end stays overdue at `now >= cap_end`, matching the existing cover comparison. Execution and reconcile budgets must be explicit non-negative integers when a session is required. A missing budget is not zero. A new entry is open only when `opened_at` is at most 5 seconds after `now`, `planned_exit` is at most `opened_at + 4h`, the exit is the same Europe/Berlin calendar day, and `planned_exit + execution budget + reconcile budget` is at or before both the 4h cap and the 20:00:00 cutoff. 20:00:00 Europe/Berlin is the committed cutoff, not an example. A missing budget is not zero.

Test: `test_ac3_session_due_and_budget`.

## AC4 — #585, restart, stale, halt, timeout

Sandbox cover uses `ledger_datetime_utc`, so the #585 Berlin-naive 5h case still caps and the 3h case does not. After due, a restart with remaining quantity reports `overdue` and `flat` false. A stale price, a halt, or a timeout on an open lot reports overdue, not flat. Quantity zero is the only flat result. Unknown halt (`None`) on the exposure report is overdue, not flat. `evaluate_short_cover` does not treat an omitted halt as a halt, so a healthy flagged lot is not force-covered.

Test: `test_ac4_timestamps_restart_and_unpriced`.

## AC5 — Weekend, calendar, funding

No new weekend scheduler. On Saturday or Sunday a new entry is denied. `weekend_admission: true` does not loosen that. The sandbox cover path applies only to lots tagged `sandbox_path = v220_paper`. Untagged lots, including stable-tier shorts and lots with no entry price, keep the existing cover behaviour when the flag is on. Stop cover still runs on Saturday with the flag on. Calendar `as_of` must be present and not in the future. Age equal to `max_age_seconds` passes; one second older is stale. A missing maintenance list is unknown, not "no maintenance". Inclusive and exclusive window edges are tested. A funding window accepts only `entry < settlement < planned_exit <= due`. A missing settlement stays missing when a caller passes an 8h interval. No funding amount is computed and the planned exit is not moved.

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
| Draft Rev 0 | 2026-11-10 | AC1–AC5 | Default-off hold, session, calendar, and funding-window contract. | Sprint package C, T220-05 | Superseded for the cutoff and 4h cap by Rev 1. |
| Draft Rev 1 | 2026-11-10 18:40 | item 3, item 6 | 20:00 Europe/Berlin Mon–Fri, 4h max, same calendar day, no weekend entry. Buffer minutes stay unset and fail closed. | Owner freeze delegated to Kay | Owner freeze. |
