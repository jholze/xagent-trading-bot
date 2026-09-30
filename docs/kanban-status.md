# Trading Bot Kanban — Status automation

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
