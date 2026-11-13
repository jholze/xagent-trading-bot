# Gate partial fill delta – Requirements

Version: 1.0 · Date: 2026-11-10 · Status: draft · Owner: Jens Holze

GitHub issue: #676. Existing in memory: RECALL_FAILED. The acceptance text in
#676 is the basis (Marcus disposition: existing ticket ACs are accepted).
This file does not edit that issue.

## Behavior

One order has one tracker state: booked quantity, trade ids, and phase.
A snapshot carries the venue cumulative quantity, the cumulative trade ids,
`finish_as`, and the requested quantity.

- The first `finish_as=open` snapshot with a positive cumulative quantity and
  at least one new trade id books that quantity and stays `open_partial`.
- A later larger cumulative snapshot books `cumulative - booked` and only the
  trade ids that were not stored. It does not book the cumulative total again.
- A larger cumulative snapshot with no new trade id is `unknown_increment`.
  State does not change and the booked quantity is not returned as zero.
- Saving the state and applying the same snapshot again books quantity 0.
  That is the restart rule.
- A cancelled remainder keeps the booked quantity, then becomes terminal.
  A later larger snapshot is `terminal` and does not add or drop quantity.
  The same terminal snapshot on restart books 0 and keeps the filled quantity.
- Unknown quantity, unknown finish, empty trade id, or a cumulative quantity
  below the booked quantity does not book.

`partial_open_needs_revisit` is true only for `partially_filled`/`partial`
with `finish_as=open`, including when `needs_reconcile` is false. It is false
for ACTIVE and for a non-partial fee-unknown row. Callers OR it with the
existing `_needs_order_reconcile`. This revision does not change that
function.

## Tests

| AC | Test |
| --- | --- |
| First partial stays tracked and books once | `tests/unit/test_gate_partial_delta_676.py::test_ac_676_first_partial_stays_tracked_and_books_once` |
| Later fill books the delta and the new trade id | `test_ac_676_later_cumulative_books_only_the_new_quantity_and_trade_id` |
| No new trade id does not book | `test_ac_676_cumulative_increase_without_a_new_trade_id_does_not_book` |
| Restart is idempotent | `test_ac_676_restart_is_idempotent_after_state_roundtrip` |
| Canceled remainder retains the fill | `test_ac_676_canceled_remainder_retains_filled_quantity` |
| Cancel can book one last delta, then keep it | `test_ac_676_cancel_snapshot_books_the_new_fill_then_keeps_it` |
| ACTIVE and fee-unknown stay on the old predicate | `test_ac_676_revisit_does_not_change_active_or_fee_unknown` |
| Unknown inputs do not book a zero | `test_ac_676_unknown_qty_finish_or_trade_id_does_not_book_zero` |

Frozen and not edited: `tests/unit/test_gate_finish_as.py::test_finish_as_open_with_fill_is_partially_filled`.

## Non-goals

No second poller. No portfolio hook (package F owns that file; this rule does
not need it). No change to `execution/gate_adapter.py` or
`execution/recovery.py` until package A hands them off. No flag hiding the
bug fix. No write to `data/`.

## Changelog

| Version | Date | AC | Change | Reason | Approved by |
| --- | --- | --- | --- | --- | --- |
| Rev 1 | 2026-11-10 | issue #676 plus restart and remainder retention | Pure delta helper and tests. Not wired. | Sprint package B. Production tracker still has the gap. | #676 ACs accepted; wiring blocked on package A |
