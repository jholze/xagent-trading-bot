---
name: release-planning
description: Use when writing or updating a release plan, a release note, or the version gate for xagent-trading-bot. Points at the repo policy. Does not assign a version, tag, merge, deploy, or approve a strategy.
---

# Release planning

Repository procedure. Not a global skill install. Not permission to execute a release.

## When to use

- Draft or revise the patch line, the Short Main-release slice, or the seasonality candidate.
- Fill [`docs/releases/RELEASE_NOTES_TEMPLATE.md`](../../releases/RELEASE_NOTES_TEMPLATE.md) for one cut.
- Answer "what version is this?" or "what is on the board for this release?"

## Read first

1. [`AGENTS.md`](../../../AGENTS.md) rules 0, 1, 4, 5, and 6.
2. [`docs/release-framework.md`](../../release-framework.md).
3. [`docs/releases/2026-11-plan.md`](../../releases/2026-11-plan.md).
4. The board view for the slice: [view 9](https://github.com/users/jholze/projects/1/views/9) patch, [view 10](https://github.com/users/jholze/projects/1/views/10) Short candidates, [view 11](https://github.com/users/jholze/projects/1/views/11) seasonality.
5. Hindsight bank `coding-agent::trading_bot` at `http://127.0.0.1:8888`. On failure write `RECALL_FAILED`. Pull request #792 is an older `staging` merge. It is not this task's recall.
6. [`docs/requirements-engineering-guide.md`](../../requirements-engineering-guide.md) §1a item 7 before turning a release idea into a build ticket. The dry run is [`docs/releases/2026-11-08-intake-tabletop.md`](../../releases/2026-11-08-intake-tabletop.md).

## Do

- Keep SemVer major, minor, and patch distinct from the owner phrase "Main release".
- Leave milestone #10 `v2.1.0` as the Klein-Live record (SHA `842ac8c`, live-candidate caps). Do not repurpose it.
- Cite the live board option `845f1cf9`, text `v2.2.0 Short + real Gate futures testnet PLANNED`, as planned metadata already set by the parent. Do not publish a tag, do not create a milestone, and do not treat #642 or #694 or #529 as a frozen build.
- Leave the maintenance number **unproved**. Do not call `v2.1.x` the running version. Do not invent a fix list for view 9.
- Treat view 10 as candidates (#694, #529) until a freeze names the scope. #694 does not revive FBR. A negative climax result stays a NO-GO for activation.
- For any `staging` → `main` acceptance, review the whole `main`…`staging` range. At the 8 November 2026 SHAs that range is 963 commits. No automatic merge.
- Separate planned, tested, staging, futures testnet, and main in the note.
- Record rollback, final-head reviews, and Jens's acceptance or the deferral.

## Stop

- No tag, no GitHub Release, no push to `staging` or `main`. A `staging` merge auto-deploys paper. A no-deploy order means the pull request stays open.
- No orders, no exchange probe, no keys, no `shorts.allow_live` change.
- No edit to `scripts/kanban_status.py` or workflows inside a docs-only release note. The script still enforces legacy labels. Views do not clear that debt.
- No strategy GO from a plan, a mock, a paper-funding function, or a config default.
- No shortening of the hold-out through 31 December 2026 or of an existing 48-hour paper measurement to fit a two-week calendar.

## Evidence

The note carries a before/after or an explicit "not run", the SHA, and the blocker if a gate is shut. `unit` still applies on the pull request. This skill does not waive it.
