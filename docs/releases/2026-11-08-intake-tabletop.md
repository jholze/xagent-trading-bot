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

## Results

| # | Stimulus | Necessary inputs before any build ticket | Verdict | Next state | Evidence used | Result |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Jens: "Add authenticated Gate futures testnet shorts for one named tenant this week and start building." | Code (`execution/gate_adapter.py` fail-closed testnet path), open and closed issues, `RECALL_FAILED`, dependencies (#412 closed, config flags), goal, measurable testnet outcomes, cost and risk, non-goals (no mainnet, no keys) | Not Ready. Raw owner sentence is intake only. | Intake, blocked. Questions only after the search: which tenant id, and whether #412 is revised. No implementation branch. | Adapter host and `_testnet_futures_allowed()`; `shorts.testnet_futures.enabled` false and empty `tenants`; #412 comment `6558128287`; fixture-only test module | Pass |
| 2 | "Build a new Gate futures testnet adapter; the old one does not count." | Same search, including closed issues | Not a rebuild. Not Ready as a new component. | Blocked intake that points at #347. A later ticket may only adapt the gap (authenticated proof is unmet) with new AC ids. | #347 closed `completed` 12 September 2026; `tests/unit/test_gate_adapter_testnet_short.py` says fixture-only, no network | Pass |
| 3 | "Make shorts better." Then a second packet that fills every canonical field for a testnet-proof ticket, and a third packet that omits the test names. | See the full check under this table. | Raw and partial: not Ready. Complete packet: Ready to implement only. | Blocked until the matched set exists. Then implement, not release. | Field-by-field walk below. | Pass |
| 4 | "Hotfix the patch line: ship seasonality calendar entries as v2.1.0, or under the board option v2.2.0, this week." | Release framework, view 11, milestone #10, the live option `845f1cf9` | Misclassified. Seasonality is not a hotfix and is not that planned Short option. Not Ready. | Intake, blocked. Stays off view 9. Milestone #10 stays Klein-Live. The board string stays a plan, not a tag. | View 11 holds #566, #567, #571, #572. Tags `v2.1.0` and `v2.2.0` were absent. Maintenance is unnumbered. | Pass |
| 5 | Frozen scope is "testnet capability only". Jens: "Also activate climax-fade and fold #529 into the same build, higher priority." | Frozen AC ids, §5, the negative study, #529's original ACs | In-place edit and a half-written revision: not Ready. A revision that keeps the NO-GO and adds only a tested #529 delta: Ready for that delta only. | Old freeze stays readable. Climax activation never becomes Ready on this evidence. | Full check below. | Pass |
| 6 | "The climax replay lost money, but `climax_fade.enabled` is already true, so approve it." | The recorded study, the config key, `short_math.funding_cost_usdt` | Not Ready. No strategy GO. A config flag is not a passed study. | Intake, blocked. No activation ticket. No profit claim. | Handoff figures (not re-run): Climax 1x n=1016, −3500.42 USDT; 2x −7000.85 USDT. Paper funding docstring is not venue reconciliation. | Pass |
| 7 | Agent files a build-ready issue with no code search and no memory line, and cites #792 as the recall. | Rule 0 recall, Existing in code, the #792 commit subject | Not Ready. Missing Hindsight line is a process violation. #792 does not fill it. | Intake, blocked, until `RECALL_FAILED` or real `memory:` / `doc:` refs are written, and Existing in code is filled. | This pass: connection to `127.0.0.1:8888` refused. #792 merged to `staging` only, SHA `65bbee356fd031015ae1200db28684bf5530853a`. | Pass |
| 8 | An open seasonality issue is shown as board Done, or someone treats a legacy label set as human Ready. | Issue state, §1a item 7, the script's decision order, the workflow `on:` list | Board Done is not delivery. Legacy Ready to Work is not human Ready. The script will not clear a wrong Done by itself. | Ticket stays not Ready. Do not edit the script in this change. Do not set the three labels. | Read 8 November 2026: #529, #566, #567, #571, #572, #694 lack the trio `omnigent:ready` + `review:lena` + `review:viktor`. `decide_status()` retains Done. Soak is applied before the parked check. | Pass |

## Scenario 3, full field check

Stimulus A, failed: "Make shorts better."

Missing: Existing in code, memory line, dependencies, measurable outcome, cost and risk, test names, non-goals, frozen ACs. Title is not a build scope.

Verdict: **not Ready**. Next state: intake, blocked. Ask only which outcome and which non-goals, after saying the search found view 10 candidates #694 and #529 and did not find a metric in the sentence.

Stimulus B, still failed: the same request plus a paragraph of intent, a dependency on #529, and `RECALL_FAILED`, but no measurable outcome and no test name.

Verdict: **not Ready**. One missing gate blocks the set.

Stimulus C, matched: English title "Record authenticated Gate futures testnet proof for one named tenant, without strategy activation." Existing in code: `execution/gate_adapter.py` `_testnet_futures_allowed`, committed `shorts.testnet_futures.enabled` false and empty `tenants`, `shorts.allow_live` false. Memory: `RECALL_FAILED`. Dependencies: #347 and #380 closed historical, #412 closed `not_planned` (not success). Goal: the unmet futures-testnet list in the November plan. Measurable outcomes: host, named tenant, SHORT, reduce-only stop, COVER or cancel, partial and uncertain fills, restart reconciliation, fees and venue funding, redacted ids, fail closed. Cost and risk: no mainnet, no keys, paper funding in `short_math` does not count. Non-goals: no climax activation, no FBR revival, no seasonality, no milestone #10 rename. ACs numbered, each with a test name, marked frozen. Safety line copied from `AGENTS.md`.

Verdict: **Ready to implement that proof ticket only, on paper.** The board was not moved by this verdict. #694 and #529 stay candidates. Not a strategy GO. Not a published tag. The live option text may be cited and still means planned, not running. CI would not have blocked stimulus A or B.

## Scenario 5, full revision check

Frozen text, Rev 0: AC-1 futures sandbox host and named tenant. AC-2 fail closed when the tenant is absent. Non-goal: no strategy activation. Tests named. Status: frozen, Ready for that narrow scope.

Stimulus A, failed: edit AC-1 in place to add climax-fade activation and mark the ticket Ready because Jens raised the priority.

Verdict: **not Ready**. §5 requires the old sentence to stay, a new id, a date, and a reason. Priority words are not a revision. In-place edit is rejected.

Stimulus B, still failed: add AC-3 "activate climax-fade" and AC-4 "fix #529" with no test names, and strike nothing. Treat the board as already accepted.

Verdict: **not Ready**. New ids without tests are untested. Climax activation contradicts the recorded NO-GO (Climax 1x −3500.42 USDT, 2x −7000.85 USDT, not re-run here). #529's original ACs are not replaced. A board column is not the gate.

Stimulus C, matched: Rev 1 dated 8 November 2026. AC-1 and AC-2 remain. AC-3 is the #529 display-index case only, with its own test name, marked `[NEW]` and untested until that test passes. Climax activation is an explicit non-goal, not an AC. `RECALL_FAILED` stays on the revision. Cost and safety lines unchanged. The revision is frozen only after those lines are present.

Verdict: **Ready for the revised capability-plus-#529 scope only after that freeze, on paper.** Climax activation stays not Ready. The previous Rev 0 text stays readable. #529's historical acceptance criteria stay. This Ready is not a board move, not a `staging` → `main` merge, and not a tag. Nothing automatic stops stimulus A.

## What this run does not show

- It does not show a futures-testnet fill, a redacted order id, or a fee reconciliation.
- It does not show automatic enforcement. A later agent can ignore the checklist. CI will not fail for a missing intake field.
- It does not show that `scripts/kanban_status.py` has stopped using the legacy labels. Scenario 8 passes because the human rule rejects the column. The script can still retain a wrong Done, move a freeze comment from Backlog to Spec/Acceptance, and move a parked issue to Shadow when a merged `staging` pull request matches the soak expression.
- It does not publish a tag and does not number the maintenance line. The live board string `v2.2.0 Short + real Gate futures testnet PLANNED` stays a plan.
- Eight manual passes mean the written checklist gave these verdicts when a person applied it. They do not mean a tool will reject the failed inputs. They do not mean a strategy or a release is accepted.
- `final_v220_readback.redacted.json` was hash-checked on the parent box. It is not in this checkout, so this note does not repeat a hash.
