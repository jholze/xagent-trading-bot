# Release framework

Status: canonical version and release policy. Date: 8 November 2026, Europe/Berlin. Owner: Jens Holze.

This file names slices, gates, and homes. It does not publish a tag, assign a SemVer, merge a branch, deploy, or approve a strategy. A skill that points here is not permission to do those things.

Operating rules remain [`AGENTS.md`](../AGENTS.md). `CLAUDE.md` is only a pointer to that file. Ticket text remains [`docs/domain/spec-writing.md`](domain/spec-writing.md) and [`docs/requirements-engineering-guide.md`](requirements-engineering-guide.md) §1a item 7, §4, and §5. A raw request is not build-ready. The manual dry run is [`docs/releases/2026-11-08-intake-tabletop.md`](releases/2026-11-08-intake-tabletop.md). Where an older product or design doc disagrees about branches, deploys, or who approves, `AGENTS.md` wins.

## Where a procedure lives

Repeatable work does not live only in a person's memory. This change does not install a skill into a global catalog, Hindsight, or any other memory store. Recall on this pass is `RECALL_FAILED` (below).

| Home | What it may decide | Path |
| --- | --- | --- |
| Repository rules | Safety, models, merges, recall | [`AGENTS.md`](../AGENTS.md) |
| Dated release decisions | Slice names, gates, calendar | this file and [`docs/releases/2026-11-plan.md`](releases/2026-11-plan.md) |
| Tickets and board | Scope and status of one piece of work | GitHub issues and the Trading Bot Kanban. This pull request does not edit them. |
| Skills | When to follow the rows above, and when to stop | [`docs/skills/release-planning/SKILL.md`](skills/release-planning/SKILL.md), [`docs/skills/ticket-spec/SKILL.md`](skills/ticket-spec/SKILL.md) |
| Scheduler | When a job fires | The scheduler that owns the job. [`docs/operations/routines.md`](operations/routines.md) is a map, not a second scheduler. |
| Release note | What one cut claims, at one SHA | [`docs/releases/RELEASE_NOTES_TEMPLATE.md`](releases/RELEASE_NOTES_TEMPLATE.md) |
| Intake dry run | Whether a raw request can become Ready | [`docs/releases/2026-11-08-intake-tabletop.md`](releases/2026-11-08-intake-tabletop.md). Manual only. |

Historical plans under `plans/` and design notes under `docs/umbau/` stay in the tree. They are not approval authority. Domain files that name a former reviewer are pinning a threshold that person wrote down. They are not a route for a merge.

## Words

| Term | Meaning here |
| --- | --- |
| SemVer major, minor, patch | The three numbers of a tag `vMAJOR.MINOR.PATCH`. Patch means a fix or mini-improvement of a feature that already exists. No new feature. |
| Main release | Jens's term for an accepted `staging` → `main` release of one frozen scope. It is not the word "major", and it does not imply a number. |
| Current patch line | Maintenance of existing features only. Board view 9. Version number blocked. |
| Next Main release | The Short slice. Board view 10 lists candidates only. No version number. Milestone v2.1.0 is not this slice. |
| Later candidate | Seasonality research. Board view 11. No version and no date. |

## Version numbers are blocked

No SemVer is assigned for the patch line or for the Short slice. This file does not invent `v1.2.1`, `v1.8.1`, `v2.0.1`, `v2.1.0`, `v2.1.1`, or `v2.1.0-rc.32`.

**Do not repurpose milestone #10 `v2.1.0` as the Short target.** Read on 8 November 2026, the milestone text is a Klein-Live freeze: first named `staging` → `main` cut, SHA `842ac8c`, live-candidate caps, later hotfixes `v2.1.x`, next staging train `v2.2.0-rc.N`. Open issues on it include [#642](https://github.com/jholze/xagent-trading-bot/issues/642) (1000 USDT spot long, tenant henry first) and [#488](https://github.com/jholze/xagent-trading-bot/issues/488). Closed [#644](https://github.com/jholze/xagent-trading-bot/issues/644) is a spec under #642. The parent left that milestone and its history in place. A Short plan that needs a number waits until lineage and a fixed scope are confirmed. Until then the board views below are the slice map, not a milestone rename.

### Inventory that blocks a guess (read 8 November 2026)

| Identifier | Object | Target | When |
| --- | --- | --- | --- |
| `staging` | Branch tip | `65bbee356fd031015ae1200db28684bf5530853a` | 5 November 2026, 23:53:55 +0100. Commit is merged pull request **#792 on `staging`, not on `main`**. |
| `main` | Branch tip | `64abf7fd5cf9781eb8b11c21a63357c9d7b0f8a3` | 9 July 2026, release pull request #3. `git rev-list --count main..staging` at these SHAs is **963**. `staging..main` is 0. |
| GitHub Release `v1.2.0` | Latest release with prerelease false. Also `v1.0`, `v1.0.0`, `v1.1.0` | annotated tag, commit `b4d2308ed287344f9eedd32d7ad3b5d7939b30d5` | 23 May 2026 |
| Git tag `v1.3.0` | Annotated tag, not in that Releases list | `9e5da150ca659cca6adb37cd45fd6b30340a01ba` | 8 June 2026 |
| Git tag `v1.5.0` | Same | `d4a0c3e32e87252f326b0c328d523daec008f6e2` | 12 June 2026 |
| Git tag `v1.6.0` | Same | `6976112d02937042ccfb916b3b3863865de4614e` | 12 June 2026 |
| Git tag `v1.7.0` | Same | `6d4326d38d7a9e9aa8fef0068cf8d79dfeecb4da` | 14 June 2026 |
| Git tag `v1.8.0` | Same | `7f89ff9ffc7ece07c31dc29bc1762656d16cd574` | 1 July 2026 |
| Git tag `v2.0-entry-guard` | Annotated tag. Commit is current `main` | `64abf7fd5cf9781eb8b11c21a63357c9d7b0f8a3` | 9 July 2026 |
| Git tags `v2.0.1-rc.1` … `v2.0.1-rc.7` | `v2.0.1-rc.7` is a lightweight tag | `c5845416bd1b6c72ae3286a19083c6c526f5f5a7` | not walked further |
| GitHub prerelease `v2.1.0-rc.31` | Newest prerelease returned | `603a6259016035256222f37df1df5223bac955ca` | 1 October 2026, 21:07 UTC |
| Tag `v2.1.0` | Absent (ref lookup 404) | — | 8 November 2026 |
| Tag `v2.0.0` | Absent from `tags/v2.0` | — | 8 November 2026 |
| Tag `v1.4.0` | Absent from `tags/v1` | — | 8 November 2026 |
| README / product docs "Version 2.0.0" / "Version 2.0" | Doc label dated 23 June 2026 | no matching tag | not a deploy |

`core/build_info.py` reports commit, branch, and dirty state. It has no SemVer. The running process and Railway were not read. `v2.1.0-rc.31` is not `staging` and not `main`.

Milestone **#9 v2.0.1** is closed. Its text says it was superseded by the `v2.1.0-rc.*` train, was not a release to `main`, and was not `--latest`.

The number may be written into a release note or a tag only when all three are true:

1. A note names the accepted base (Release, annotated tag, or commit) with object type and commit SHA. The table is the inventory, not that choice.
2. The fixed scope of that cut is written down (issue and pull-request ids, or an explicit empty patch list). View 9 currently has no such list, and this file does not invent one.
3. Jens accepts the number before the tag exists, and the number does not repurpose milestone #10.

## Board views (parent readback, 8 November 2026, 14:14)

These are the slice map. They are not milestone assignments and not version numbers. This session's Project item query returned no visible items, so the URLs are the parent's readback, not a second card-level confirmation from this token. This pull request does not edit the board.

| Slice | View | What the parent read back |
| --- | --- | --- |
| Current maintenance patch | [view 9](https://github.com/users/jholze/projects/1/views/9) | Patch. Version assignment blocked. No invented fix list. |
| Next Main release, Short | [view 10](https://github.com/users/jholze/projects/1/views/10) | Candidates [#694](https://github.com/jholze/xagent-trading-bot/issues/694) and [#529](https://github.com/jholze/xagent-trading-bot/issues/529) only. |
| Later conditional seasonality | [view 11](https://github.com/users/jholze/projects/1/views/11) | [#566](https://github.com/jholze/xagent-trading-bot/issues/566), [#567](https://github.com/jholze/xagent-trading-bot/issues/567), [#571](https://github.com/jholze/xagent-trading-bot/issues/571), [#572](https://github.com/jholze/xagent-trading-bot/issues/572). |

Parent readback on status, not re-written here: open seasonality cards that had said Done were corrected to Backlog: Epics (#566) or Backlog (#567, #571, #572), with no Ready label. Closed children #568, #569, #570, and #573 were left closed. Historical #347, #380, and #412 were left unchanged. #694 and #529 kept their original acceptance criteria; a dated English planning addendum was added on the issues. Those issue bodies remain the scope text. This file does not replace them.

New views do not mean the legacy board automation is clean. See the debt below.

## Owner decisions that are in force

1. **8 November 2026, 13:54–13:58.** Current line: patches and mini-improvements of existing features only. No new features. Next Main release: Short trading plus authenticated real Gate.io futures testnet from `staging`. Mocks do not satisfy that. Seasonality is a later candidate, outside the Short release. Freeze ticket scope before acceptance. A change after freeze is a dated revision. The first five `staging` → `main` releases after this decision are with Jens. Every merge to `main` stays Jens only.
2. **14:09.** Procedures have durable homes in the repository. Skills point at the rules. They are not a second policy.
3. **14:14 board audit.** Milestone #10 stays Klein-Live. Versions stay blocked until lineage and fixed scope are confirmed. Use the three views above. No strategy GO.

## Gates that stay shut

A Short Main release is not accepted, and no strategy is a GO, until every item below is true. Missing evidence stays blocked.

1. Fixed scope with exact ids, or a written decision that view 10's candidates are the whole scope. #694 is a parked FBR reactivation rule, not an implementation. #529 is an open `/short` display-index bug. Neither is Ready. Choosing a strategy versus testnet-capability-only is still open. The recorded climax-fade result is negative ([plan](releases/2026-11-plan.md)). That blocks any profitability claim and any silent return of climax-fade or FBR.
2. Version gate above. Milestone #10 is not reused as the Short number.
3. Authenticated futures-testnet acceptance in the plan. Fixture tests are not that proof. Spot sandbox is not assumed to stand in for futures.
4. #412 stays closed `not_planned` (1 November 2026, comment `6558128287`: shorts parked, no futures testnet work planned). Closed is not testnet success. This pull request does not reopen it.
5. Reviews on the final head of the release diff: DeepSeek on every pull request, Claude Opus on a high-risk path (`AGENTS.md` rule 1). The diff under review is the whole `main`…`staging` range (963 commits at the SHAs above), not only the Short patches. There is no automatic merge.
6. Observation rules stay whole. [`docs/domain/hypothesis-protocol.md`](domain/hypothesis-protocol.md) holds E1/E2 and the hold-out from 6 November 2026 00:00 Europe/Berlin through 31 December 2026. Per-change 48-hour paper measurements in `docs/evidence/` are not shortened to fit 8–22 November. #694's promote text (n ≥ 100 and at least three out-of-sample folds) is marked in the issue as a proposal, not a frozen law.
7. A release note from the template, at the final SHA, with rollback and Jens's acceptance.
8. Jens merges `staging` → `main`, with him, for the first five such releases after 8 November 2026.

Dates in the plan are decision slots. They do not run the work.

## Safety this policy does not relax

- No push to `staging` or `main`. A merge to `staging` auto-deploys the paper bot (`xagent-test`). This documentation pull request stays open.
- No tag and no GitHub Release.
- No live trading, no orders, no withdraw, no exchange probe, no reading of keys.
- No edit to `config.json`, workflows, or `scripts/kanban_status.py` in this change.
- `shorts.allow_live` stays false in committed config. This file does not flip it.

## Legacy rules versus what still runs

Retired for humans, in `AGENTS.md` (1 November 2026, updated 4 and 5 November) and pointed to from `CLAUDE.md`: do not set `omnigent:ready`, `review:lena`, `review:viktor`, or `veto:*`. Do not wait on Kay, Marcus, Lena, or Viktor as the approval chain. Review is DeepSeek per pull request and Claude Opus for the final high-risk review. Two model families. Author and reviewer are not the same family.

That retirement is not a cleaned automation. Still in the tree, and not modified here:

- [`scripts/kanban_status.py`](../scripts/kanban_status.py) constants `omnigent:ready`, `review:lena`, `review:viktor`, `veto:lena`, `veto:viktor` (lines 64–68). `ready_chain_green()` is true only when the first three are present and neither veto is. `decide_status()` can set Status to Ready to Work on that chain (step 4 in the function's own comment). The module docstring still says Kay alone sets `omnigent:ready`. `SPEC_EVENT_RE` still matches `marcus spec`. If current Status is Done, the function returns "retain Done".
- [`.github/workflows/kanban-status.yml`](../.github/workflows/kanban-status.yml) runs that script on issue opened, labeled, unlabeled, reopened, issue comment created, pull request closed, and manual dispatch. It does not itself set labels. A later event can still move Status using the legacy chain.
- CI job `unit` is a different gate (`classify`, pytest unless the trusted base says the diff is documentation, `review-gates`, hypothesis evidence, changed-file Ruff). It does not read Lena or Viktor labels.

**Blocker:** views 9–11 and a documentation banner do not disable the script. Claiming the board automation is clean would be false until a later change, outside this docs-only pull request, updates `scripts/kanban_status.py` and is reviewed on its own. [`docs/kanban-status.md`](kanban-status.md) keeps the old column text as a description of that unedited script and says not to follow it as a human rule.

`DOCUMENTATION.en.md` §17 and `DOCUMENTATION.md` §17 still contained a `feature/*` / `deploy_staging.sh` / push-`staging` checklist. Those sections are marked historical in this change. The sentences are still in the file so the old instruction can be audited. They are not to be run.

## Pull request #792 is a different change

#792 merged to **`staging`**, not `main`, at `65bbee356fd031015ae1200db28684bf5530853a` (5 November 2026, 23:53:55 +0100). Its subject is the Hindsight recall gate and the Omnigent workspace clone. That merge and its review belong to #792.

This release-policy pull request is later. It does not inherit #792's review, does not claim a healthy Omnigent workspace, and does not claim a successful recall. On 8 November 2026 a call to `http://127.0.0.1:8888` was refused. Result: **`RECALL_FAILED`**. No `memory:` id. Mem0 tool discovery failed in this session and is not a substitute. Rule 0 stays in force: if a memory and a doc disagree, git plus `AGENTS.md` plus committed `config.json` win.

## Revisions

| Rev | Date | Change |
| --- | --- | --- |
| 0 | 8 November 2026 | Policy created. Versions blocked. Milestone #10 left as Klein-Live. Slices point at board views 9, 10, and 11. No strategy GO. No tag. Automation debt recorded and left in the script. Intake stays in the requirements guide; the tabletop is a manual dry run. |
