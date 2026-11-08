# Trading Bot Kanban — Status automation

**Human rule (8 November 2026).** Approval is [`AGENTS.md`](../AGENTS.md). Do not set `omnigent:ready`, `review:lena`, `review:viktor`, or `veto:*`, and do not wait on Lena, Viktor, Marcus, or Kay as that chain. Build-ready means [`docs/requirements-engineering-guide.md`](requirements-engineering-guide.md) §1a item 7, not the Ready to Work column below. A board Done card is not delivery. This page does not authorize a deploy, a main merge, or a strategy activation.

**Automation debt, still true, not fixed here.** [`scripts/kanban_status.py`](../scripts/kanban_status.py) `ready_chain_green()` still requires `omnigent:ready` + `review:lena` + `review:viktor` and no veto, and `decide_status()` can move Status to Ready to Work on that chain. [`.github/workflows/kanban-status.yml`](../.github/workflows/kanban-status.yml) still runs that script. Those files are out of scope for a documentation-only change. New board views do not disable the script. CI job `unit` does not read those labels. Until a later reviewed change updates the script, do not claim the labels are gone or that the automation is clean.

Manual readback on 8 November 2026, not a workflow run: open issues #529, #566, #567, #571, #572, and #694 do not carry all three legacy labels together. The script has no branch that sets Done. If Status is already Done, `decide_status()` returns "retain Done", so a wrong Done stays until a person moves it. The workflow runs on issue opened, labeled, unlabeled, reopened, issue comment created, pull request closed, and manual dispatch. An ordinary issue-body edit does not match those types, and a project-field or view edit does not either. A comment that matches the freeze / fix-round expression can move an empty or Backlog card to Spec/Acceptance. On a merged pull request into `staging`, a soak match calls `soak_merged_staging=True` before the parked check, so a parked issue can still move to Shadow. The redacted hash bundle at `/workspace/umbau/release_structure_2026-11-08/` was not in this checkout and was not re-hashed here.

The sections below are the historical description of that unedited script. They are not instructions to follow.

Board: [Trading Bot Kanban](https://github.com/users/jholze/projects/1/views/1)
Repo: `jholze/xagent-trading-bot`
Dev branch: `staging`. Prod branch: `main` (never merge / push here).

Automation moves **project Status columns only**. It does not set, remove, or
change labels (`omnigent:ready`, `review:lena`, `review:viktor`, `veto:*`,
`sprint:*`, `parked`, …). Kay alone sets `omnigent:ready` after the Ready
chain.

## Columns

| Status | Meaning |
|---|---|
| **Backlog** | New / not started / parked with a named cause |
| **Spec/Acceptance** | Spec freeze or acceptance / fix-round |
| **Ready to Work** | `omnigent:ready` + `review:lena` + `review:viktor`, no `veto:*` |
| **Shadow** | Merged to staging for paper/shadow soak; issue stays OPEN (≤ ~30 days) |
| **Backlog: Epics** | Epic cards only (retained, not a work column) |
| **Done** | Retained; not used for soak |

## When Status moves

1. New issue / no Status → **Backlog** (epic label → **Backlog: Epics**).
2. Comment with spec freeze / Marcus spec / fix-round → **Spec/Acceptance**.
   A template `## Acceptance criteria` heading is not enough.
3. Kay set `omnigent:ready` **and** both review labels, no veto → **Ready to Work**.
   Without `omnigent:ready` the card stays put.
4. PR **merged to `staging`** for a paper/shadow soak → **Shadow**. Issue stays
   OPEN. PRs are never cards on the board. PRs targeting `main` are ignored.
5. Veto added (or `omnigent:ready` removed) while on Ready to Work → back to
   **Spec/Acceptance**.
6. Coding starts only from **Ready to Work** + `omnigent:ready` (label → worker).
   Chat requests are not a second route.

## Wiring

- GitHub Action: `.github/workflows/kanban-status.yml`
- Policy + GraphQL: `scripts/kanban_status.py`
- Repo secret **`KANBAN_PROJECT_TOKEN`**: PAT with `project` and `repo`.
  `GITHUB_TOKEN` cannot write this user-owned project.

Manual reconcile (Status only):

```bash
python3 scripts/kanban_status.py apply --issue 618 --dry-run
python3 scripts/kanban_status.py apply --issue 618
```

Actions → Kanban status → Run workflow → issue number.

## Work rules at Ready to Work

When Status is Ready to Work **and** `omnigent:ready` is present:

- Branch from `staging`
- Implement the frozen English acceptance criteria on the issue
- PR targeting `staging` only
- `allow_live` / live trading / keys / `shorts.allow_live` / withdraw / Gate
  futures stay OFF unless Jens asked in writing
- Paper / read-only / shadow is default
- GitHub text (PR title/body/commits/comments) in English
- After a soak merge to staging: Status → Shadow; do not close; do not set
  Ready-chain labels

Never: set `omnigent:ready`, override `veto:lena` / `veto:viktor`, push or
merge to `main`, live orders, or a parallel chat side-quest for the same ticket.
