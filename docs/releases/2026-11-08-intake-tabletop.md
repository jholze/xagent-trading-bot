# Intake tabletop, 8 November 2026

Manual simulation. Not an automated test, not a pytest run, not a runtime test, not a Gate testnet test, and not a profit claim. No order was sent. No build was run. No deploy was run.

Rule under test: [`docs/requirements-engineering-guide.md`](../requirements-engineering-guide.md) §1a item 7, which uses [`docs/domain/spec-writing.md`](../domain/spec-writing.md) and §4–§5 of the same guide. No second ticket template. Operating limits stay [`AGENTS.md`](../../AGENTS.md).

Two different things are in play. The board option `845f1cf9` already reads `v2.2.0 Short + real Gate futures testnet PLANNED`, and [view 10](https://github.com/users/jholze/projects/1/views/10) was updated, by the parent's 14:24 MEZ readback. #694 and #529 have Planning-P2 dated addenda and still keep their historical acceptance criteria. Both are candidates, not Ready. No milestone was created. #642 and milestone #10 are unchanged. Maintenance is unnumbered. That metadata is live. It is not a commit in this pull request.

The checklist below is proposed repository text in an unmerged pull request. Applying it by hand does not install a bypass check.

Method: for each stimulus, apply §1a item 7 by hand. This is a manual process simulation. It is not end-to-end enforcement. Nothing in CI or in `scripts/kanban_status.py` checks these fields, and this change does not add that check. A person or agent can still skip the checklist. The simulation does not claim otherwise.

A failed or partial input stays **not Ready**. A supplied input is Ready only when every matched gate below is present. Ready means the ticket may be implemented. It does not publish `v2.2.0`, does not prove testnet, and does not approve a strategy.

Canonical fields (the gates): English title; Existing in code; Existing in memory or `RECALL_FAILED`; dependencies; user goal; measurable outcomes; cost, risk, and test evidence; non-goals; numbered observable ACs with test names, frozen under §5; safety limits when the work can touch orders or keys. A new feature labeled hotfix fails. A negative strategy result labeled as activation fails. A board column does not pass.

An earlier metadata pass scored incomplete intake and priority reacceptance as partial, because those two checks stopped at "blocked" and did not walk a completed field set. Scenarios 3 and 5 below are that walk.

Hindsight for this pass: **`RECALL_FAILED`** (`http://127.0.0.1:8888` refused). That line is an input to scenario 7, not a successful recall. Pull request #792 is already on `staging` and is not this simulation's authority.

A routine paper analysis that does not ask for a code change is outside this Ready gate. It still cannot skip `docs/domain/hypothesis-protocol.md`. That distinction was checked so item 7 would not become a new permission layer. It is not a ninth scenario.

Second manual walk, 8 November 2026, after the independent audit of head `7bfc2a4`. Scenarios 3, 5, and 7 are written out in full below. This walk is still a proposal in the unmerged pull request. It does not install a check.

## Results

| # | Stimulus | Necessary inputs before any build ticket | Verdict | Next state | Evidence used | Result |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Jens: "Add authenticated Gate futures testnet shorts for one named tenant this week and start building." | Code (`execution/gate_adapter.py` fail-closed testnet path), open and closed issues, `RECALL_FAILED`, dependencies (#412 closed, config flags), goal, measurable testnet outcomes, cost and risk, non-goals (no mainnet, no keys) | Not Ready. Raw owner sentence is intake only. | Intake, blocked. Questions only after the search: which tenant id, and whether #412 is revised. No implementation branch. | Adapter host and `_testnet_futures_allowed()`; `shorts.testnet_futures.enabled` false and empty `tenants`; #412 comment `6558128287`; fixture-only test module | Pass |
| 2 | "Build a new Gate futures testnet adapter; the old one does not count." | Same search, including closed issues | Not a rebuild. Not Ready as a new component. | Blocked intake that points at #347. A later ticket may only adapt the gap (authenticated proof is unmet) with new AC ids. | #347 closed `completed` 12 September 2026; `tests/unit/test_gate_adapter_testnet_short.py` says fixture-only, no network | Pass |
| 3 | A raw "Make shorts better." B, the second packet, still omits test names. C, the third packet, is the complete fictional example. | See [Scenario 3](#scenario-3). Order is A, then B, then C. | A and B: not Ready. C: paper-Ready to implement only. No test was run. | A and B stay blocked. C may be implemented on paper. Not Done. | Full fictional packet below. New test names are hypothetical. | Pass |
| 4 | "Hotfix the patch line: ship seasonality calendar entries as v2.1.0, or under the board option v2.2.0, this week." | Release framework, view 11, milestone #10, the live option `845f1cf9` | Misclassified. Seasonality is not a hotfix and is not that planned Short option. Not Ready. | Intake, blocked. Stays off view 9. Milestone #10 stays Klein-Live. The board string stays a plan, not a tag. | View 11 holds #566, #567, #571, #572. Tags `v2.1.0` and `v2.2.0` were absent. Maintenance is unnumbered. | Pass |
| 5 | Frozen scope is "testnet capability only". Jens: "Also activate climax-fade and fold #529 into the same build, higher priority." | Frozen AC ids, §5, the negative study, #529's original ACs | In-place edit and a half-written revision: not Ready. A revision that keeps the NO-GO and adds only a tested #529 delta: Ready for that delta only. | Old freeze stays readable. Climax activation never becomes Ready on this evidence. | Full check below. | Pass |
| 6 | "The climax replay lost money, but `climax_fade.enabled` is already true, so approve it." | The recorded study, the config key, `short_math.funding_cost_usdt` | Not Ready. No strategy GO. A config flag is not a passed study. | Intake, blocked. No activation ticket. No profit claim. | Handoff figures (not re-run): Climax 1x n=1016, −3500.42 USDT; 2x −7000.85 USDT. Paper funding docstring is not venue reconciliation. | Pass |
| 7 | A: no memory line, cites #792. B: `RECALL_FAILED` plus an owner request for a seasonality research GO. C: described only, a successful recall that did not happen. | See [Scenario 7](#scenario-7). | A and B: blocked, no GO. A real recall is not claimed. | B stays blocked. The chain #567 then #571 then #572 does not start. | Connection to `127.0.0.1:8888` refused. #792 is not this recall. | Pass |
| 8 | An open seasonality issue is shown as board Done, or someone treats a legacy label set as human Ready. | Issue state, §1a item 7, the script's decision order, the workflow `on:` list | Board Done is not delivery. Legacy Ready to Work is not human Ready. The script will not clear a wrong Done by itself. | Ticket stays not Ready. Do not edit the script in this change. Do not set the three labels. | Read 8 November 2026: #529, #566, #567, #571, #572, #694 lack the trio `omnigent:ready` + `review:lena` + `review:viktor`. `decide_status()` retains Done. Soak is applied before the parked check. | Pass |

## Scenario 1

Raw sentence only. Tenant id is not chosen. #412 is not revised. Verdict: **not Ready**, intake blocked. The answers to those two questions appear only inside the fictional packet in [Scenario 3](#scenario-3), stimulus C. They are not answered by the raw sentence.

## Scenario 2

Duplicate lookup, from the issue read already recorded in this plan, not a new search: [#347](https://github.com/jholze/xagent-trading-bot/issues/347) closed `completed`. The component is `execution/gate_adapter.py`. Verdict: **not Ready** as a new adapter. A later ticket may only extend the unmet authenticated proof.

## Scenario 3

Order is fixed: A raw, B partial, C complete fiction. B is not the complete packet.

### A, raw, blocked

Stimulus: "Make shorts better."

Missing every gate: title, Existing in code, memory line, dependencies, goal, measurable outcome, cost, risk, non-goals, AC-to-test map. Verdict: **not Ready**.

### B, second packet, still blocked

Adds intent, a dependency on #529, and `RECALL_FAILED`. Still no measurable outcome and no test name. Verdict: **not Ready**. One missing gate keeps the block.

### C, third packet, complete fictional example

Label: **simulation**. This packet was not filed as an issue. No test in it was executed. No board card moved.

| Field | Simulated content |
| --- | --- |
| Title | Record authenticated Gate futures testnet proof for simulated tenant `sim-tenant-alpha`, without strategy activation |
| Tenant answer | `sim-tenant-alpha` is fictional. It is not in committed `shorts.testnet_futures.tenants` (that list is empty). It is applicable only inside this example. It is not a configured tenant and not a reuse of a live tenant id. |
| Reuse answer | Do not build a new adapter. Reuse `GateExecutionAdapter._testnet_futures_allowed` and `_GATE_TESTNET_HOST`. |
| Duplicate lookup | Real issue ids from the earlier read: #347 closed `completed` (adapter), #380 closed `completed` (reduce-only stop), #412 closed `not_planned`, comment `6558128287` (parked, not success). Simulated outcome: extend proof; do not reopen #412 in this example. |
| Existing in code | `execution/gate_adapter.py`; `shorts.allow_live` false; `shorts.testnet_futures.enabled` false; tenants empty |
| Memory | `RECALL_FAILED` |
| Goal | Show one fictional tenant can be proved on the futures sandbox later, or be rejected fail-closed |
| Scope | The AC rows below |
| Non-goals | No climax activation, no FBR revival, no seasonality, no milestone #10 rename, no mainnet, no key read |
| Dependencies | #347, #380, #412 as history. #694 and #529 stay candidates and are not this packet. |
| Risk | Wrong host could touch mainnet. Empty tenant list must keep rejecting. Paper funding is not venue funding. |
| Safety | `AGENTS.md`: no orders in this simulation, no keys, `allow_live` stays false |
| Evidence | **No executed test evidence.** Rows below are a map, not a run. |

| AC | Observable result in the fiction | Test name | What that name is |
| --- | --- | --- | --- |
| AC-1 | A SHORT for `sim-tenant-alpha`, when listed, is aimed at `https://api-testnet.gateapi.io` and not at mainnet | `test_sim_ac1_named_tenant_futures_host` | Hypothetical. Not in the tree. Not written. Not run. |
| AC-2 | An empty tenant list rejects before any create | `test_testnet_empty_tenants_rejected` | Real name in `tests/unit/test_gate_adapter_testnet_short.py`. Fixture-only. **Not executed in this simulation.** Not authenticated proof. |
| AC-3 | A tenant absent from the list is rejected | `test_testnet_tenant_not_in_allowlist_rejected` | Real name, same file. **Not executed here.** |
| AC-4 | A filled testnet SHORT can attach a reduce-only stop, and COVER cancels that stop id | `test_sim_ac4_reduce_only_stop_and_cover_cancel` | Hypothetical. Not in the tree. Not run. The file does contain `test_cover_cancels_stored_stop_id`; that existing name was not run either and is not substituted as AC-4's result. |
| AC-5 | A partial fill and an uncertain create stay consistent after a simulated restart | `test_sim_ac5_partial_and_uncertain_restart` | Hypothetical. Not run. |
| AC-6 | Fees, venue funding, and position size are compared to a redacted venue payload | `test_sim_ac6_venue_fee_funding_position` | Hypothetical. Not run. `short_math.funding_cost_usdt` is paper funding and is not this evidence. |

Verdict for C: **paper-Ready to implement this fictional spec only.** Not Done, because no mapped test was run. Not a strategy GO. Not a published tag. The live option `v2.2.0 Short + real Gate futures testnet PLANNED` may be cited and still means planned. CI would not have blocked A or B.

## Scenario 4

Seasonality filed as a hotfix under `v2.1.0` or under option `845f1cf9` is misclassified. Verdict: **not Ready**. Maintenance stays unnumbered. The planned name is not a hotfix vehicle.

## Scenario 5

Simulation of a revision. Not a real approval and not a filed change.

### Rev 0, frozen, before the priority change

Scope: futures-sandbox proof for `sim-tenant-alpha` only. Non-goal: no strategy activation. Memory: `RECALL_FAILED`.

| ID | Frozen text | Test mapping | Execution |
| --- | --- | --- | --- |
| AC-1 | Listed fictional tenant uses the futures sandbox host and not mainnet | `test_sim_ac1_named_tenant_futures_host` | Hypothetical. Not run. |
| AC-2 | Missing tenant fails closed before create | `test_sim_ac2_missing_tenant_fail_closed` | Hypothetical. Not run. A similar real test, `test_testnet_tenant_not_in_allowlist_rejected`, exists and was not run. |

Rev 0 paper-Ready is only for that pair, and only as a spec. It is not Done.

### Stimulus A, blocked

Jens raises priority and asks to edit AC-1 in place so it also activates climax-fade, then mark Ready.

Verdict: **not Ready**. §5 keeps AC-1's old sentence. Priority is not a revision. In-place edit is rejected. Lifecycle: stays on Rev 0, blocked for the new request.

### Stimulus B, still blocked

Draft adds AC-3 "activate climax-fade" and AC-4 "fix #529" with no test names.

Verdict: **not Ready**. Untested new ids do not reaccept the ticket. Climax activation contradicts the recorded NO-GO (Climax 1x −3500.42 USDT, 2x −7000.85 USDT, not re-run). #529's historical ACs stay. Lifecycle: revision draft, blocker open, not reaccepted.

### Rev 1, frozen fiction, 8 November 2026

AC-1 and AC-2 stay, same ids, same hypothetical mappings, still not run. Climax activation is a non-goal, not an AC. Changed scope: add only the #529 display-index delta.

| ID | Rev 1 text | Test mapping | Execution |
| --- | --- | --- | --- |
| AC-1 | unchanged from Rev 0 | `test_sim_ac1_named_tenant_futures_host` | Hypothetical. Not run. |
| AC-2 | unchanged from Rev 0 | `test_sim_ac2_missing_tenant_fail_closed` | Hypothetical. Not run. |
| AC-3 | `[NEW 2026-11-08]` After the watchlist changes between coin tap and size tap, the size tap still arms the symbol chosen at the coin tap. This is the #529 case. #529's original ACs are not rewritten. | `test_sim_ac3_short_size_tap_keeps_chosen_symbol` | Hypothetical. Not in the tree. Not written. Not run. |

Reacceptance lifecycle: Rev 0 remains readable. Rev 1 is paper-Ready to implement AC-3 only after this table exists. AC-3 stays untested, so the revision is **not Done** and **not approved**. Climax activation stays a blocker and never becomes Ready on this evidence. Nothing automatic stops stimulus A.

## Scenario 6

Config `climax_fade.enabled: true` plus a negative study is not approval. Verdict: **not Ready**. No GO. No profit claim.

## Scenario 7

### A, missing line, blocked

No Existing in code and no memory line. The writer cites #792. Verdict: **not Ready**. #792 is the 5 November `staging` merge. It is not a recall for this request.

### B, `RECALL_FAILED` plus a research GO, still blocked

The memory line is present and reads `RECALL_FAILED` (this pass: `http://127.0.0.1:8888` refused). Jens then says "research GO, start the seasonality build."

Verdict: **blocked, no GO.** A failed recall does not become permission. Seasonality still waits on a Short closeout and a Hindsight-infrastructure closeout, then #567, then #571, then #572, and the hold-out through 31 December 2026 still applies. A partial packet that has only the recall line, or only the GO sentence, keeps the block.

### C, not performed

A successful recall did not happen. On paper, a valid one would cite `memory:<id>` or `doc:<id>` that agrees with git, or would record the contradiction and follow git. Even then, implementation would stay not Ready until the closeouts, the #567 → #571 → #572 order, the hold-out, frozen ACs, and a named unrun-or-real test map were all present. That description is not a GO and not evidence that a recall succeeded.

## Scenario 8

Board Done on an open research issue is not delivery. The legacy Ready to Work column is not human Ready. Verdict: **not Ready**. The script is unchanged: it retains Done, a freeze comment can move Backlog to Spec/Acceptance, and a merged `staging` soak pull request can Shadow a card before the parked check. Open issues #529, #566, #567, #571, #572, and #694 did not have all three legacy labels together on the 8 November read.

## What this run does not show

- It does not show a futures-testnet fill, a redacted order id, or a fee reconciliation.
- It does not show automatic enforcement. A later agent can ignore the checklist. CI will not fail for a missing intake field.
- It does not show that `scripts/kanban_status.py` has stopped using the legacy labels. Scenario 8 passes because the human rule rejects the column. The script can still retain a wrong Done, move a freeze comment from Backlog to Spec/Acceptance, and move a parked issue to Shadow when a merged `staging` pull request matches the soak expression.
- It does not publish a tag and does not number the maintenance line. The planned name `v2.2.0 Short + real Gate futures testnet PLANNED` is already assigned on the board and is still not a tag, not acceptance, and not runtime.
- The eight verdicts above are this second hand walk. They do not mean a tool will reject the failed inputs, and they do not accept a strategy or a release.
- The independent audit reported SHA256 `7028ce54f756a9202e85029307c4dcf1d2b8f06c66fc47c61fc748880a7f8a71` for `final_v220_readback.redacted.json`. That file is not in this checkout, so the hash was not recomputed here.
