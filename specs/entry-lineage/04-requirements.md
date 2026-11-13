# Entry indicator lineage – Requirements

Version: 1.1 · Date: 2026-11-10 · Status: in review · Owner: Kay (draft D-LIN, not a GitHub issue)

Stable ID: D-LIN. Not issued. Do not treat this file as a new #600 or #582 epic.

RECALL_FAILED: Hindsight bank `coding-agent::trading_bot` is only reachable on Jens' Mac loopback (`http://127.0.0.1:8888`). This cloud VM cannot call it. No memory id is cited.

## Authority

| Revision | Date | What changed | Who |
|---|---|---|---|
| Rev 0 | 2026-11-10 | Draft AC-LIN-1..5 in the sprint file. Not issued. | Sprint draft. Marcus outcome was still pending. |
| Rev 1 | 2026-11-10 18:21–18:40 Europe/Berlin | Marcus disposition recorded in the sprint file: "F OK". Owner freeze the same evening does not set the 90% runtime figure; that target stays observation-only. This revision freezes the acceptance tests below and the wiring rules. It does not add #600 snapshot keys. | Marcus check cited from `sprint_2026-11-10` (Kay coordinates). Owner delegated the freeze sheet to Kay; items 6, 8 and 11 stay open and are not used here. |

Rev 0 text stays in the sprint attachment. This file does not rewrite it in place.

## Problem

`entry_snapshot` already exists (#600, fixed key set). The first confirmed fill does not receive the decision-time RSI, ATR, volume factor, source, rationale, or size fields that the decision already held. Later reads must not rebuild those numbers from the current HUD.

## Non-goals

- No new snapshot keys. #600 whitelist stays.
- No second tape. Extended BB / RelVol rank / social / oracle / memory context, when the decision already supplied it, goes in the versioned `entry_lineage` block (`schema: d-lin-1`), not into `entry_snapshot`.
- No entry-policy, sizing, or unknown-data trade change. A missing indicator stays null. The buy still fills.
- No live/mainnet path, no key, no tenant config edit, no backfill, no fake fill.
- Runtime ">=90% of new lots" is not measured and is not a pass/fail in this change.
- No adapter edit while package B owns `execution/`. The fill already receives `order_id`. Portfolio reads the decision block from that order row.

## Flag

This change completes the existing #600 contract (pass through values the decision already has). It is not a new execution capability and is not hidden behind a default-off flag. Missing indicators do not deny the trade. `shorts.allow_live` is untouched.

## AC-LIN-1

Given decision inputs RSI 81, ATR 2, volume factor 3, source `lc`, the first confirmed fill stores those same numbers under the existing #600 keys (`rsi`, `atr`, `volume_factor`, `source`) plus timeframe, rationale codes when a list was supplied, and size fields when supplied. Nothing is fetched or recomputed at fill time. A later partial, DCA, or replay does not replace the first snapshot. A failure while attaching the snapshot or the lineage block logs a warning and does not drop the confirmed fill.

Test: `tests/unit/test_entry_lineage_wiring.py::test_ac_lin_1_first_fill_snapshot`
and `test_ac_lin_1_partial_dca_keeps_snapshot`, `test_ac_lin_1_snapshot_failure_keeps_fill`, `test_ac_lin_1_no_remote_recompute`.
Frozen regression: `tests/unit/test_entry_snapshot_600.py` (assertions unchanged).

## AC-LIN-2

Buys whose order source is exactly `lc`, `cmc`, `auto`, `entry_sensor_15m`, or `gainer_relvol` keep that token on the new lot's `entry_source`. Other channels (`manual`, `grid`, `x`, `dca_scheduled`) stay untagged, so they do not stamp `entry_at`. Existing gainer tags from `_default_entry_source` still apply. `entry_at` is the same wall clock as `first_buy_at`. The local-vs-mongo position normalizer ignores `entry_at` for that reason. Snapshot `source` still records the order source #772 already stores. A missing RSI, ATR, or volume factor is null and `entry_lineage.missing.<field>` names the reason. No fabricated 45 / 3.0 / 1.0 default. `atr_pct` is not written into snapshot `atr` (different quantity). It is kept under `entry_lineage.extended.atr_pct` when the decision carried it. Extended context keys are only the supplied whitelist. They are not #600 keys.

A DCA add onto an already open lot does not backfill `entry_source` when the lot had none. Only the first fill of a new lot receives the channel token. `entry_guard` still matches only the configured source list (committed default: `entry_sensor_15m`).

Test: `test_ac_lin_2_source_and_missingness_matrix`, `test_ac_lin_2_extended_context_not_in_snapshot_keys`.

## AC-LIN-3

Each exit allocation stores `entry_allocation_id` and `entry_order_id` taken from the lot's stored lineage. Two partial sells of one entry share that id. A multi-lot exit keeps every supplied allocation. An exit with no stored id is unlinked. The linker does not search by symbol and time. A pre-trade reject gets `attempt_id` and `attempt_only: true`. Its projected `is_fill` is false even if a filled timestamp is present. A later order may store `prior_attempt_id`. The rejected row stays a non-fill.

Test: `test_ac_lin_3_entry_exit_and_attempt_links`, `test_ac_lin_3_multi_lot_allocations`, `test_ac_lin_3_rejected_attempt_is_not_a_fill`.

## AC-LIN-4

Read-only MCP lots/orders project the captured snapshot, the public lineage block, and non-secret ids for the selected tenant only. Legacy lots without a snapshot stay loadable and project null. The projection does not recompute indicators. Embeddings, tokens, and secret-like keys are dropped.

Test: `test_ac_lin_4_mcp_projection_and_tenant_isolation`, `test_ac_lin_4_legacy_lot_has_null_snapshot`.

## AC-LIN-5

`lineage_coverage` reports per-source totals, the missing-field denominator, missing reasons, and how many exit rows carry an allocation id. Fully supplied fixture lots keep their fields (100% of that fixture set). Exit rows that have an allocation id are linked (100% of the fixture exits that were given ids). `runtime_target_measured` is false. The report does not invent fills.

Test: `test_ac_lin_5_daily_check_export`.

Roundtrip: `test_lineage_roundtrip_serialize_restart`.
Risk rebuild seam: `test_risk_rebuild_keeps_decision_fields`.

## Wiring rules (Rev 1)

1. Decision observations are copied onto `TradeOrder` before risk. Risk rebuilds a new `TradeOrder` and drops unknown fields. `copy_decision_lineage` puts only `decision_*` observations and `prior_attempt_id` back on the approved order. It does not change `usdt_amount`, `entry_15m_vol_ratio`, or `strategy_tier`. Those last two stay with the risk rebuild and #772.
2. `entry_15m_vol_ratio` is not restored after the rebuild. The entry guard must not see a ratio the risk rebuild dropped.
3. The order row stores `entry_lineage` (`d-lin-1`). Portfolio `execute_buy` / `execute_short` apply it on the first fill only. Direct callers may pass `lineage=`.
4. Package B does not need an adapter kwarg for the normal path: `_sync_local_ledger` already passes `order_id`. If a fill is booked with no order row and no `lineage=`, indicators stay null.
5. `strategy_tier` stamping stays with #772. This revision does not add snapshot keys and does not set the tier.

## Changelog

| Version | Date | AC | Change |
|---|---|---|---|
| 0.1 | 2026-11-10 | AC-LIN-1..5 | Rev 0 draft, sprint only |
| 1.1 | 2026-11-10 | AC-LIN-1..5 | Rev 1 freeze of tests and wiring rules after Marcus "F OK". 90% runtime target not measured. No #600 key added. |
