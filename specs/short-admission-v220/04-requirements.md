# Short sandbox admission – Requirements

Version: 0.2 · Date: 2026-11-10 · Status: draft · Owner: Jens Holze

Local draft id: T220-03 (not a GitHub issue).

## Rev 1 — 2026-11-10 18:40 Europe/Berlin

Owner freeze, delegated to Kay. Supersedes the pending note for items 5, 7 and 9 only. Item 6 (time buffer) stays parameterized on the expiry contract and is not given a minute value here. Items 1, 2, 4, 8, 10, 11 and 12 are not implemented.

Paper/testnet start defaults, used only when `shorts.sandbox_admission_v220.enabled` is boolean true:

- Leverage 1x. Any other explicit leverage is denied.
- Minimum size is the exchange minimum quantity. Unknown or smaller quantity is denied.
- New short exposure, including pending, uncertain and reserved notionals, is at most 10% of virtual equity. Exact 10% passes. One supplied quote quantum over fails.
- Stop loss at most 0.25% of equity per position, and 0.25% plus other short stops at most 0.75%.
- `shorts.max_margin_pct` stays 20 in committed config. A sandbox figure above 20% or above the frozen 10% / 0.25% / 0.75% ceilings is `cap_loosened`, not applied. `leverage_default` stays 2 for the existing path.
- Regime missing, unknown, or stale denies. Stale means the caller did not supply `regime_as_of` and `regime_max_age_seconds`, or the age is greater than that supplied maximum. No age limit is invented. Every regime cell starts closed (`opened_regimes` empty). A cell admits only when that profile lists it.
- `product_kind` `stock_token`, missing, or `unknown` denies a new short (`short_path_deferred`). `stock_exclusion_applied` stays false. A `perp` candidate is not denied for a sibling stock. Reduce-only cover stays allowed.

A missing profile (no mapping, or a required account slot absent or `unknown`) still denies and is not filled with zero equity. The flag still defaults off. `config.json` is not edited.

Rev 0 below is unchanged history. Its `owner_freeze_pending` outcome is replaced by the rules above.

## Rev 0 — 2026-11-10 Europe/Berlin

Pending Marcus check on the original draft text. The numbers below were pending until Rev 1.

Hindsight: RECALL_FAILED. Recall is only reachable via the Mac loopback, not from this cloud VM.

Marcus, 2026-11-10, binding for this revision: the numbers and the regime table in AC-03a/b/c are pending owner freeze. Until that freeze, the only admission behavior is "missing risk profile = deny", plus parameterized plumbing and tests. Do not copy values from Owner-Freeze-Werte.md. That file is not in this checkout and its figures are not repeated here. AC-03d (tokenized-stock exclusion) is not implemented; it conflicts with the sprint prompt and needs the owner freeze. This file records it as a draft note only.

Existing ticket behavior that this revision does not change: #385 (closed) finite-leverage margin cap in `risk/risk_manager.py` is not edited. #386 (closed) stop/liquidation leverage clamp stays in `strategies/short_math.py`. `shorts.allow_live` stays false. `shorts.climax_fade.enabled` is not a grant for this path.

## Problem

There is no sandbox admission contract that refuses a short entry when the risk profile is missing. `resolve_short_params` fills tier defaults (including a 4h time cap and leverage) for the existing paper path. Those defaults stay in place for that path. The new path must not invent a profile, a regime decision, or a stock ban.

## Non-goals

- No numeric limits, clearance buffer, quantum table, or regime matrix from AC-03a/b/c. Those wait for the owner freeze.
- No tokenized-stock exclusion and no per-candidate stock eligibility gate (AC-03d draft note below).
- No edit to `risk/risk_manager.py`, `execution/`, portfolio files, or `config.json`.
- No new global cap, kill switch, or max-open number (Guide section 1a vs R4 is unresolved).
- No live, mainnet, or testnet order. No key. No tenant write.
- No change to existing short cover, math, or policy defaults in this revision.

## AC1 — Missing flag is off

`shorts.sandbox_admission_v220.enabled` is on only when that value is boolean `true`. A missing key, `false`, or any other value leaves the capability off. `shorts.climax_fade.enabled: true` does not turn it on. `shorts.allow_live` is not switched on by this flag.

Test: `tests/unit/test_v220_t220_03.py::test_ac1_flag_missing_or_not_true_denies`.

## AC2 — Missing risk profile denies

When the flag is on, a missing risk profile denies entry. Missing means: no profile argument, a non-mapping, an empty mapping, or any required slot absent, blank, or the word `unknown`. Required slots are `profile_id`, `leverage`, `notional`, `margin`, `stop_risk`, `nav`, `metadata`, and `reservations` (`pending`, `uncertain`, `reserved`). A missing slot is not stored as zero, as an empty reservation, or as the existing paper leverage default. Float values are not an exact profile and count as missing. The decision `admitted` is false and `action` is `none`.

Test: `test_ac2_missing_risk_profile_denies` and `test_ac2_missing_slot_is_not_filled`.

## AC3 — Supplied parameters are plumbed and not decided

When every required slot is an exact string, integer, or finite `Decimal`, the decision echoes those values unchanged. Nested `metadata` and `reservations` are echoed in stable key order. Extra caller keys, including a regime label, are echoed and not interpreted. `regime_applied` is false for every label. A supplied profile does not admit: AC-03a/b/c numbers and the regime table are pending, so the reason is `owner_freeze_pending` unless a safety reason below already applies. Two different regime labels produce the same reasons.

Test: `test_ac3_supplied_profile_is_plumbed_not_admitted` and `test_ac3_regime_label_is_not_a_table`.

## AC4 — Live posture and missing mode proof deny

The sandbox path denies when `shorts.allow_live` is true, when the mode proof says `live` true, or when `market` is not `paper` or `futures_usdt_testnet`. A missing or incomplete mode proof denies with `mode_proof_missing` and is not treated as paper. Proof fields are echoed when supplied. No exchange host is hardcoded and no age limit is invented here (numeric staleness stays with the pending freeze and with package B). An existing long is not flipped: `action` stays `none` and the reason includes `long_not_flipped` unless the long quantity is an exact zero. Reduce-only cover of an existing short stays permitted when entry is denied.

Test: `test_ac4_live_and_missing_proof_deny`, `test_ac4_long_is_not_flipped`, `test_ac4_reduce_only_cover_stays_permitted`.

## AC5 — AC-03d is not implemented

A candidate marked `stock_token` and a candidate marked `perp` get the same admission reasons. `stock_exclusion_applied` is false. This module has no global or per-symbol stock ban.

Test: `test_ac5_stock_token_is_not_excluded`.

## AC-03d draft note (not implemented)

Rev1 proposed an initial tokenized-stock exclusion. The 2026-11-10 sprint prompt says stock tokens stay product-allowed and that a global exclusion is not approved. Marcus (2026-11-10) says AC-03d conflicts with that prompt and needs the owner freeze, so it must not be implemented. This revision does not deny a candidate for being a stock token, and it does not add a missing-calendar/halt/depth eligibility rule that would act as that exclusion. A later revision can add per-candidate eligibility only after the owner freeze, and only without a blanket product ban.

## AC-03a/b/c parked (not implemented)

The following stay out of code and out of test fixtures until the owner freeze. No figure from Owner-Freeze-Werte.md is copied:

- Validated numeric leverage, notional, margin, and stop-risk limits, units, and precision, including exact-at-limit and one-quantum-over comparisons.
- Pending and uncertain reservation arithmetic against those limits.
- Clearance `L - S >= max(tick, b * (L - E))` and any concrete `b` or tick.
- The regime admission matrix and its source-age limit.
- Squeeze, ADL-warning, and stale-risk decisions that depend on that table.

Plumbing distinction only: the four risk slots and the reservation slot are separate fields. They are not summed and not defaulted.

## Trace

| AC | Test | This revision |
| --- | --- | --- |
| AC1 flag off | `test_ac1_flag_missing_or_not_true_denies` | implemented |
| AC2 missing profile denies | `test_ac2_missing_risk_profile_denies`, `test_ac2_missing_slot_is_not_filled` | implemented |
| AC3 plumb, do not decide | `test_ac3_supplied_profile_is_plumbed_not_admitted`, `test_ac3_regime_label_is_not_a_table` | implemented |
| AC4 live, proof, long, cover | `test_ac4_*` | implemented |
| AC5 no stock exclusion | `test_ac5_stock_token_is_not_excluded` | implemented, exclusion not built |
| AC-03a/b/c numbers and regime table | none | pending owner freeze |
| AC-03d stock exclusion | draft note only | pending owner freeze |

## Evidence

Metric: count of admission decisions that admit when the profile is missing, and count of regime-table lookups. Target after this revision: both zero. `config.json` `shorts.allow_live` stays false. `risk/risk_manager.py` stays unchanged.

## Changelog

| Version | Date | AC | Change | Reason | Approved by |
| --- | --- | --- | --- | --- | --- |
| Draft Rev 0 | 2026-11-10 | AC1–AC5 | Missing-profile deny and parameter plumbing only. AC-03a/b/c numbers and regime table not copied. AC-03d recorded, not implemented. | Marcus correction 2026-11-10; sprint package C | Superseded for items 5, 7 and 9 by Rev 1. |
| Draft Rev 1 | 2026-11-10 18:40 | items 5, 7, 9 | Start defaults: 1x, exchange min qty, 10% / 0.25% / 0.75%, 20% cap not raised, all regime cells closed, short-path stock deferral. | Owner freeze delegated to Kay | Owner freeze. Item 6 still has no minute value. |
