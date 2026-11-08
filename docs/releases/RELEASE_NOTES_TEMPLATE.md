# Release notes template

Copy this into the note for one cut. Delete no heading. A blank or "blocked" line stays blank until the evidence exists. Creating this file does not publish a release.

Policy: [`docs/release-framework.md`](../release-framework.md). Calendar: [`docs/releases/2026-11-plan.md`](2026-11-plan.md).

## Identity

| Field | Value |
| --- | --- |
| Slice | current patch line / next Main release (Short) / other (name it) |
| SemVer | Maintenance line: **unnumbered** until the lineage gate passes. Do not write a `v2.1.x` runtime. Short slice: board option `845f1cf9`, text `v2.2.0 Short + real Gate futures testnet PLANNED`. Not a tag until scope is frozen and Jens accepts a tag. Do not reuse milestone #10. |
| Git tag | none until Jens accepts the number |
| Branch | |
| Commit SHA | full 40-character SHA of the reviewed head |
| Board view | [view 9](https://github.com/users/jholze/projects/1/views/9) or [view 10](https://github.com/users/jholze/projects/1/views/10) or other URL |
| Supersedes | previous note or "none" |

## Status of this cut

Fill each row with evidence or with "not done". Do not copy a later row into an earlier one.

| Stage | State | Evidence |
| --- | --- | --- |
| Planned | | issue ids, or "no fix list" |
| Tested | | command, SHA, result |
| Staging (paper) | | deploy SHA if a merge was allowed; otherwise "not merged" |
| Futures testnet | | redacted ids, or "not run" |
| Main | | Jens merge SHA, or "not merged" |

## Scope

- In scope (issue and pull-request ids):
- Excluded:
- Strategy choice, if any: not a GO unless the plan's negative finding is answered by a new pre-registered study at this SHA
- User-visible change:

## Evidence

- Tests or backtest, with fees:
- Futures testnet, if this cut claims it: host, named tenant, no mainnet orders, SHORT, reduce-only stop, COVER or cancel, partial and uncertain fills, restart reconciliation, fees, funding, position truth, fail-closed result
- Config fingerprint (names and non-secret values only):
- `main`…`staging` commit count at the SHAs above, and who reviewed that whole range:

## Reviews at the final head

| Role | Model id | Head SHA | Result |
| --- | --- | --- | --- |
| Per-PR review | `deepseek-v4-pro` | | |
| Final review (required on a high-risk path and before main) | `claude-opus-5-5` | | |
| Tester | separate from the coder | | |

Unresolved findings:

## Risks and rollback

- What breaks if this cut is wrong:
- How to roll back (commit or flag), without quoting a secret:
- What was not checked:

## Owner acceptance

- Jens, date, accept or defer:
- If deferred, the blocker and the next slot:
- Next line (name only, no invented version):

## History

| Rev | Date | Change |
| --- | --- | --- |
| 0 | | note opened |
