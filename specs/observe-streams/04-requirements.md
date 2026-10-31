# Observe streams — requirements

Rev 1. 2026-10-31. Frozen before the build in this change.

Observe only. No order sizing, entry, exit, stop, lock, or limit change. `fire_enabled` for macro stress stays off and is not applied.

## AC1

Each shadow/observe JSONL stream is mirrored to its own Mongo collection: `obs_buy_decision_tape`, `obs_daily_gainer_desk`, `obs_cap_order_observe`, `obs_shadow_signal`, `obs_shadow_exit`, `obs_shadow_link`, `obs_shadow_control`. Each collection has a 120-day TTL index on `timestamp` and an index on `tenant_id`. The JSONL file is still written.

Acceptance test: `tests/unit/test_observe_streams.py::test_each_observe_stream_is_readable_from_its_collection`, `test_ttl_and_tenant_indexes`.

## AC2

A Mongo failure or a blocked insert does not raise into the caller and does not wait on the insert. The JSONL line is still there.

Acceptance test: `test_blocking_mongo_insert_does_not_stall_append`, `test_mongo_error_does_not_escape_and_jsonl_remains`, `test_full_queue_drops_without_blocking`.

## AC3

Daily gainer desk counters reconcile with rows appended: `hit + late + missed + rejected == n == sum`. A later failed append does not count rows that did not land and does not omit rows that did. The same emit key twice in one call is one row.

The assertion on `tests/unit/test_macro_stress_observe_625.py::test_defaults_observe_and_fire_false` changes in Rev 1 because observe now defaults on (AC4). It used to assert `observe_enabled` is false and that an empty config returns no record.

Acceptance test: `tests/unit/test_gainer_desk_tally_630.py::test_emit_tally_matches_rows_including_a_failed_later_append`, `test_duplicate_key_in_one_call_is_one_row`, `test_mixed_batch_buckets_sum_to_rows_written`. `test_scrape_shape_is_an_invariant_violation` pins the 2026-10-25 scrape shape as inconsistent.

## AC4

Macro stress observe defaults to on when the config section is missing. `fire_enabled` defaults to false. A test cycle with no `macro_stress_observe` key emits one observe line. The order decision matches the same cycle with observe forced off.

Acceptance test: `test_defaults_observe_and_fire_false`, `test_default_observe_emits_a_line_and_does_not_change_the_order_decision`, `test_default_observe_does_not_change_a_risk_decision`.

## AC5

`docs/observability/observe-streams.md` names each collection, the fields, and a read-only `find`.

Acceptance test: `test_docs_name_every_collection_and_a_read_only_query`.

## AC6

`shadow_link` is stored in `obs_shadow_link`, not in `obs_shadow_signal`.

Acceptance test: `test_shadow_link_is_not_stored_as_shadow_signal`.

## Evidence

Before/after is in `docs/evidence/issue-630.md`. Metric: streams readable from Mongo, gainer-desk tally violations on a fixture, macro observe lines on a test cycle.
