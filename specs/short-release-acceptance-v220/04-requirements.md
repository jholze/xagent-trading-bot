# Short release acceptance – Requirements

Version: 0.2 · Date: 2026-11-10 · Status: draft · Owner: Jens Holze

Draft id: D-E2E. Not issued. Hindsight: `RECALL_FAILED` (Mac loopback only; this VM recorded the failure and did not invent a recall).

Marcus disposition 2026-11-10 18:21 Europe/Berlin: package H is OK to implement on the existing draft. Fixture results are never an authenticated testnet pass. Do not reopen or close #412. No new global kill switch.

Owner freeze 2026-11-10 18:40 Europe/Berlin, delegated by Jens to Kay: items 1–5, 7, 9, 10 and 12 are paper/testnet start values. Items 6 (time buffer), 8 (permit minutes and amounts) and 11 (exhaustion thresholds) stay open and fail closed. This package does not write `config.json` and does not change `shorts.allow_live`.

## Rev 0 acceptance text (unchanged)

<!-- AC-E2E-CANONICAL-BEGIN -->
AC-E2E-1: Given scanner candidate fixture, enabled paper/testnet fixture tenant and valid mode proof, run normal risk→SHORT→confirmed partial/full fill→protective stop→time-cap COVER→ledger/MCP trace. Refer to owner contracts, assert causal IDs and net arithmetic, no external network or data/ writes.
AC-E2E-2: Given disabled flag/live/wrong tenant/stale data/Redis timeout/unknown fee/stop reject/uncertain create/restart/orphan/partial cover race, assert contract-owned failure result, no duplicate/reversal or unauthorized emergency flatten; regular authorized reduce-only management remains possible.
AC-E2E-3: Given evidence rows classified fixture/manual/actual with candidate SHA, AC revision/hash, model and test status, any stale SHA, missing real funding proof or missing required review yields NOT VERIFIED/NO-GO. Passing offline harness cannot output authenticated success.
AC-E2E-4: Given no explicit operation manifest, acceptance command cannot contact an exchange or enable a tenant. Prepare only a disabled manual runbook/manifest for a future exact tenant/origin/actions/budget/time/SHA authorization and evidence-redaction checklist; no automatic authentication/credential discovery.
<!-- AC-E2E-CANONICAL-END -->

The sha256 of that text is the `ac_revision_hash` stamped on evidence rows.

## Rev 1 — 2026-11-10 phase boundary

Phase 1, this revision, adds the offline judge under `tools/short_release_acceptance/`, the integration harness, the evidence schema, and the disabled runbook. It does not edit `services/trading_service.py`, orchestrator hooks, `execution/gate_adapter.py`, F's models, or `config.json`.

The production path named in AC-E2E-1 stays unwired (`INTEGRATION_WIRED` is false). A linked fixture is a rehearsal: `rehearsed_short_intents` may be 1, `production_new_entries` is 0, and the release verdict stays `NOT_VERIFIED`. Predecessor modules that are absent are `blocked_dependencies`. Their decision logic is not reimplemented here.

**pending Marcus check:** treating AC-E2E-1's production run as a later integration, while this revision pins the fixture rehearsal and the refusal to call it acceptance. The AC sentences above are unchanged.

### Gates that are in force now

- `short_release_acceptance.enabled` must be literal `true`. Missing, false, `shorts.enabled`, and `shorts.climax_fade.enabled` do not open the rehearsal.
- `mode` `live`, `shorts.allow_live` true, or a malformed mode produces no rehearsed intent. Paper, `futures_testnet`, and `testnet` are the only fixture modes. Mode proof needs isolated one-way, leverage 1, and `readback` true. A set-response is not proof.
- Every production regime cell stays shut (`production_regime_admission` false). Stock-token and unknown asset classes are denied for a new short. That is a phase-1 exclusion, not a product ban written into config.
- Scanner input is raw features. `signal` must be null and `threshold_status` must be `unmeasured`. No exhaustion threshold is applied.
- Clearance uses frozen `b = 0.05` on supplied venue entry, liquidation, tick, and stop. Boundary gap passes. One tick closer fails. Missing venue data or a modeled liquidation does not admit.
- Holding must be finite, positive, at most 4 hours, and below 24 hours. The legacy climax cap of 16 hours does not satisfy this rehearsal. Entry is before 20:00 Europe/Berlin; exit is at or before 20:00; weekdays only; same civil day. Naive timestamps fail closed.
- `execution_buffer_minutes` has no frozen number. A manifest must carry the key and the value null. Any invented minute value is rejected. The window is not executable.
- Ledger components must sum to `reported_net`. An unknown component forbids a numeric net, including 0. `|ledger - venue| <= 0.01` USDT passes; 0.01 plus one cent fails. A wider tolerance is rejected.
- `#600` snapshot keys are cited, not extended. Exit links need entry order, lot, and exit ids.
- Emergency flatten is not accepted. Permit minutes and amounts are open. Reduce-only cover stays allowed when entries are denied.
- Evidence kind `fixture` or `manual` cannot be an authenticated pass. A stale SHA is `NO-GO`. Missing review, tester, or real venue funding proof is `NOT_VERIFIED`. Offline producers cannot be upgraded by editing `kind`. This package cannot set `acceptance_executed_by_this_package`.
- #412 stays `closed_not_planned`. A row that says reopen or close is `NO-GO`.
- No new global kill switch. The manifest rejects a kill-switch action.

## Existing coverage (not owned here)

| Surface | Where it already lives | This revision |
| --- | --- | --- |
| Testnet host constant, fixture routing tests | `execution/gate_adapter.py`, `tests/unit/test_gate_adapter_testnet_short.py` | Not edited. Manifest origin is the frozen `:443` form. The adapter constant has no explicit port; B owns that. |
| 4h default, cover still allowed, `allow_live` false | `strategies/short_policy.py`, `config.json` | Not edited. Climax `time_cap_hours` 16 and `climax_fade.enabled` true are not this path's switch. |
| Entry snapshot keys | `strategies/positions.py` `_ENTRY_SNAPSHOT_KEYS` | Cited. Equality is tested. Schema is not widened. |
| T220-01 / #676 / T220-02 / freeze module | Open PRs #804, #805, #806, #807 | Not imported and not copied. Reported as missing until they are on the candidate HEAD. |
| Admission, lineage wiring, net booking | No merged C/F/G module | `blocked_dependencies`. |
| #412 | Closed `NOT_PLANNED` | Not reopened or closed. |
| #764 | Open. Freeze recommends `deny` for staging; main stays with Jens | Not changed. Remains a blocker. |

## AC-to-test map

| AC | Test | What it pins |
| --- | --- | --- |
| AC-E2E-1 | `tests/integration/test_short_release_offline.py::test_ac_e2e_1_scanner_to_cover` | Linked fixture chain, net 9.83, zero venue calls, zero `data/` writes, verdict `NOT_VERIFIED` |
| AC-E2E-1 | `test_ac_e2e_1_gates_deny_new_entry` | Missing/false flag, live, climax-only, malformed mode, stock token, stale source, production scope |
| AC-E2E-1 | `test_ac_e2e_1_chain_rejects_inconsistent_fixtures` | Tenant leak, duplicate fill, unknown cost, tolerance, clearance, 4h boundary, snapshot keys |
| AC-E2E-2 | `test_ac_e2e_2_fault_matrix` | Twelve faults, no duplicate/reversal/flatten, reduce-only cover remains, missing owner result is not a pass |
| AC-E2E-3 | `test_ac_e2e_3_evidence_kind_and_sha_guard` | Kind, stale SHA, funding proof, reviewers, offline producer, #412 |
| AC-E2E-4 | `test_ac_e2e_4_no_operation_without_manifest` | No manifest and a complete manifest both refuse transport, tenant enable, and secret discovery |
| AC-E2E-4 | `test_ac_e2e_4_manifest_rejects_unfrozen_or_secret_values` | Host, budget, buffer, reserved tenants, secrets, kill switch |
| AC-E2E-1 | `test_entry_snapshot_keys_match_issue_600` | Cited `#600` keys |
| AC-E2E-3 | `test_canonical_ac_text_matches_spec` | AC hash source equals this file |
| AC-E2E-4 | `test_harness_imports_no_trading_or_network_stack` | No trading-service, adapter, or network imports |

## Non-goals

- Authenticated testnet orders, key discovery, account-mode changes, tenant flips, ledger reset, deploy.
- Wiring D's candidate into the trading service. That is a later exclusive integration after B/C/D/F contracts are on one HEAD.
- A second exchange client, a second ledger, or a new global kill switch.
- A profitability claim. The 2026-11-05 short replay remains NO-GO and is not recomputed here.
- Editing or merging PR #794.

## Changelog

| Version | Date | AC | Change | Reason | Approved by |
| --- | --- | --- | --- | --- | --- |
| Rev 0 | 2026-11-10 | AC-E2E-1..4 | Draft text, not issued | Sprint D-E2E | Marcus: H OK at 18:21; not an activation approval |
| Rev 1 | 2026-11-10 | AC-E2E-1..4 unchanged | Phase-1 judge, schema, disabled manifest. Production hook stays off | Launch phase split while B/C/F are open | Phase split pending Marcus check; freeze 18:40 used as start values |
