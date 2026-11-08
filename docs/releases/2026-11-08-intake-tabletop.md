# Intake tabletop, 8 November 2026

Manual simulation. Not an automated test, not a pytest run, not a runtime test, not a Gate testnet test, and not a profit claim. No order was sent. No build was run. No deploy was run.

Rule under test: [`docs/requirements-engineering-guide.md`](../requirements-engineering-guide.md) §1a item 7, which uses [`docs/domain/spec-writing.md`](../domain/spec-writing.md) and §4–§5 of the same guide. No second ticket template. Operating limits stay [`AGENTS.md`](../../AGENTS.md).

Method: for each stimulus, apply the checklist by hand. Necessary inputs are what the rule requires before any other move. The verdict is what the rule requires. Next state is where the work sits after the verdict. Evidence is the repo fact used, not a new measurement. Pass means the written rule produces that verdict. Fail would mean the rule lets a raw or incomplete request through as Ready.

Hindsight for this pass: **`RECALL_FAILED`** (`http://127.0.0.1:8888` refused). That line is an input to scenario 7, not a successful recall. Pull request #792 is already on `staging` and is not this simulation's authority.

A routine paper analysis that does not ask for a code change is outside this Ready gate. It still cannot skip `docs/domain/hypothesis-protocol.md`. That distinction was checked so item 7 would not become a new permission layer. It is not a ninth scenario.

## Results

| # | Stimulus | Necessary inputs before any build ticket | Verdict | Next state | Evidence used | Result |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Jens: "Add authenticated Gate futures testnet shorts for one named tenant this week and start building." | Code (`execution/gate_adapter.py` fail-closed testnet path), open and closed issues, `RECALL_FAILED`, dependencies (#412 closed, config flags), goal, measurable testnet outcomes, cost and risk, non-goals (no mainnet, no keys) | Not Ready. Raw owner sentence is intake only. | Intake, blocked. Questions only after the search: which tenant id, and whether #412 is revised. No implementation branch. | Adapter host and `_testnet_futures_allowed()`; `shorts.testnet_futures.enabled` false and empty `tenants`; #412 comment `6558128287`; fixture-only test module | Pass |
| 2 | "Build a new Gate futures testnet adapter; the old one does not count." | Same search, including closed issues | Not a rebuild. Not Ready as a new component. | Blocked intake that points at #347. A later ticket may only adapt the gap (authenticated proof is unmet) with new AC ids. | #347 closed `completed` 12 September 2026; `tests/unit/test_gate_adapter_testnet_short.py` says fixture-only, no network | Pass |
| 3 | "Make shorts better." | Search still required. Goal metric, non-goals, cost, strategy choice, and AC tests are absent. | Not Ready. Missing material requirements. | Intake, blocked. Ask only the load-bearing gaps (which outcome, which non-goals), not a blank "what should we build?" | No measurable outcome in the sentence. View 10 candidates #694 and #529 are not this sentence. | Pass |
| 4 | "Hotfix the patch line: ship seasonality calendar entries as v2.1.0 this week." | Release framework, view 11, milestone #10 text, version inventory | Misclassified. A new research slice is not a hotfix. Not Ready. No version assigned. | Intake, blocked. Stays off view 9. Milestone #10 stays Klein-Live (SHA `842ac8c`, live-candidate caps). | View 11 holds #566, #567, #571, #572. Tag `v2.1.0` is absent. Patch numbers are blocked. | Pass |
| 5 | Frozen scope is "testnet capability only". Jens: "Also activate climax-fade and fold #529 into the same build." | The frozen AC ids, §5 revision rules, the negative study, #529's existing ACs | Not Ready. A post-freeze add is a revision, not an in-place edit, and not build approval. | Previous freeze stays readable. New ids only, marked untested, blocked until the revision is frozen. Climax activation stays refused. #529's original ACs stay. | §5 forbids reusing an id. Climax study recorded as NO-GO in the November plan. #529 is a candidate, not Ready. | Pass |
| 6 | "The climax replay lost money, but `climax_fade.enabled` is already true, so approve it." | The recorded study, the config key, `short_math.funding_cost_usdt` | Not Ready. No strategy GO. A config flag is not a passed study. | Intake, blocked. No activation ticket. No profit claim. | Handoff figures (not re-run): Climax 1x n=1016, −3500.42 USDT; 2x −7000.85 USDT. Paper funding docstring is not venue reconciliation. | Pass |
| 7 | Agent files a build-ready issue with no code search and no memory line, and cites #792 as the recall. | Rule 0 recall, Existing in code, the #792 commit subject | Not Ready. Missing Hindsight line is a process violation. #792 does not fill it. | Intake, blocked, until `RECALL_FAILED` or real `memory:` / `doc:` refs are written, and Existing in code is filled. | This pass: connection to `127.0.0.1:8888` refused. #792 merged to `staging` only, SHA `65bbee356fd031015ae1200db28684bf5530853a`. | Pass |
| 8 | An open seasonality issue is shown as board Done, or legacy labels would move another card to Ready to Work. | Issue state, §1a item 7 Ready list, `scripts/kanban_status.py` `decide_status` | Board Done is not delivery. The legacy Ready to Work column is not human Ready. Do not implement. | Ticket stays not Ready. Board correction stays with the parent. Do not edit the script in a docs-only change. Do not set `omnigent:ready`, `review:lena`, or `review:viktor`. | Parent readback: #566 Backlog: Epics; #567, #571, #572 Backlog; no Ready. `ready_chain_green()` still requires those three labels and no veto. `unit` CI does not read them. | Pass |

## What this run does not show

- It does not show a futures-testnet fill, a redacted order id, or a fee reconciliation.
- It does not show that `scripts/kanban_status.py` has stopped using the legacy labels. Scenario 8 passes because the human rule rejects the column, while the script debt remains.
- It does not assign a SemVer and does not freeze view 9 or view 10.
- Eight passes means the written checklist rejected eight bad promotions. It does not mean a strategy or a release is accepted.
