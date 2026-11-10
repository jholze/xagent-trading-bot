# Short sandbox admission – Requirements

Version: 0.2 · Date: 2026-11-10 · Status: draft · Owner: Jens Holze

Local draft id: T220-03 (not a GitHub issue).

## Rev 1 — 2026-11-10 18:40 Europe/Berlin

Owner freeze, delegated to Kay. Supersedes the pending note for items 5, 7 and 9 only. Item 6 (time buffer) stays parameterized on the expiry contract and is not given a minute value here. Items 1, 2, 4, 8, 10, 11 and 12 are not implemented.

Paper/testnet start defaults, used only when `shorts.sandbox_admission_v220.enabled` is boolean true:

- Leverage 1x. Any other explicit leverage is denied.
- Quantity must equal the exchange minimum. Above or below that quantity is denied.
- Total short exposure is open short notional plus pending, uncertain, and reserved plus the candidate. The total is at most 10% of virtual equity. Exact 10% passes. One supplied quote quantum over fails. Missing open notional denies.
- Stop loss at most 0.25% of equity per position, and that stop plus other short stops at most 0.75%.
- `shorts.max_margin_pct` stays 20. A configured or profile cap above 20% is denied. `leverage_default` stays 2 for the existing path. NAV at or below zero denies.
- Regime cells open only from `shorts.sandbox_v220.paper_start.opened_regimes` in config. The committed list is empty, so every cell denies. A caller `opened_regimes` value does not open a cell and is denied. Regime age uses the config ceiling (60 seconds). A caller cannot raise it. A missing clock (`now` is none) denies. A future or older-than-ceiling `regime_as_of` denies.
- Mode proof uses `strategies/short_mode_proof.py` (same allowed/reason/margin_mode/position_mode result as PR #804; that module is not on staging). Age above 60 seconds denies. `margin_mode` must be `isolated` and `position_mode` `one_way`.
- Missing or unknown `existing_side` denies. A non-numeric `existing_qty` denies. A long with no quantity denies. A long with positive quantity is not flipped.
- `product_kind` `stock_token`, missing, or `unknown` denies a new short (`short_path_deferred`). `stock_exclusion_applied` stays false. Reduce-only cover stays allowed.

A missing profile still denies and is not filled with zero equity. The admission flag still defaults off. `paper_start` is committed with every regime cell closed; it does not enable the flag.

## Rev 0 — superseded

Rev 0 (2026-11-10, before the owner freeze) required only a missing-profile deny and did not admit. That text is not the current contract. Do not use its old test names or its `owner_freeze_pending` outcome.

Historical Rev 0 detail is omitted here so it cannot be read as the current contract.

## Trace (current)

| Check | Test |
| --- | --- |
| Flag off | `test_ac1_flag_missing_or_not_true_denies` |
| Missing profile | `test_ac2_missing_risk_profile_denies`, `test_ac2_missing_slot_is_not_filled`, `test_ac2_each_absent_slot_denies` |
| 1x and caps | `test_freeze_exact_limits_and_one_quantum`, `test_ac2_omitted_leverage_uses_start_default` |
| Total exposure | `test_total_exposure_includes_open_book_and_reservations` |
| Exact minimum quantity | `test_freeze_min_qty_and_unknown_book` |
| Closed cells and caller cannot open | `test_freeze_regime_cells_start_closed` |
| NAV, clock, margin cap, side, proof age | `test_nav_now_side_and_margin_cap_mutants` |
| Live, long, cover | `test_ac4_live_and_missing_proof_deny`, `test_ac4_long_is_not_flipped`, `test_ac4_reduce_only_cover_stays_permitted` |
| Stock deferral, not a global ban | `test_ac5_stock_deferral_is_not_a_global_ban` |

## Evidence

Metric: count of admission decisions that admit when the profile is missing, and count of regime-table lookups. Target after this revision: both zero. `config.json` `shorts.allow_live` stays false. `risk/risk_manager.py` stays unchanged.

## Changelog

| Version | Date | AC | Change | Reason | Approved by |
| --- | --- | --- | --- | --- | --- |
| Draft Rev 0 | 2026-11-10 | AC1–AC5 | Missing-profile deny and parameter plumbing only. AC-03a/b/c numbers and regime table not copied. AC-03d recorded, not implemented. | Marcus correction 2026-11-10; sprint package C | Superseded for items 5, 7 and 9 by Rev 1. |
| Draft Rev 1 | 2026-11-10 18:40 | items 5, 7, 9 | Start defaults: 1x, exchange min qty, 10% / 0.25% / 0.75%, 20% cap not raised, all regime cells closed, short-path stock deferral. | Owner freeze delegated to Kay | Owner freeze. Item 6 still has no minute value. |
