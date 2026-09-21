# Working agreement for this project

Project docs: see `README.md`, `docs/umbau/README.md` (Umbau-Konzept, 7 Phasen), `DOCUMENTATION.md` / `DOCUMENTATION.en.md`, and `docs/hermes/HERMES_DOCUMENTATION.md` for background before making non-trivial changes.

## Delegation policy: Claude plans & reviews, Grok writes code

This project uses the `grok-build` plugin (`/plugin install grok-build@xai-grok-build`) to delegate implementation
work to the Grok CLI. The intended split:

- **Claude (this session):** understands the request, reads the relevant code, breaks it into concrete, well-scoped
  coding tasks, decides what to delegate vs. do directly, and reviews/tests everything that comes back before it's
  considered done.
- **Grok (`/grok-build:delegate "<task>"`):** does the actual code-writing for tasks that are substantial and
  well-specified — e.g. a multi-file feature, a large refactor, writing a new module or a batch of tests, or anything
  long/mechanical that doesn't need step-by-step judgment calls.

Keep small, exploratory, or judgment-heavy edits (a one-line fix, deciding *what* the strategy should do, anything
where the spec is still fuzzy) in Claude directly — only hand Grok tasks once they're concrete enough to describe in
a paragraph.

### Commands

| Command | Use |
|---------|-----|
| `/grok-build:check` | Verify the Grok CLI is installed, authenticated and reachable |
| `/grok-build:delegate "<task>"` | Hand a coding task to Grok — **edits files by default** |
| `/grok-build:review` | Grok code review against local git state |
| `/grok-build:critique` | Grok challenges the implementation approach and design choices |
| `/grok-build:runs` | List active and recent runs for this repo |
| `/grok-build:show [run-id]` | Show the stored final output of a finished run |
| `/grok-build:stop [run-id]` | Stop an active background run |

`delegate` flags: `--background` / `--wait` (default: foreground; prefer `--background` for long or open-ended work),
`--resume` / `--fresh` (continue the current Grok thread or start a new one), `--model <model>`,
`--effort <low|medium|high>`.

### Safety guardrail — this bot trades with real state

`aria_bot.py`, `execution/`, `risk/`, and the `*.live.json` files (`positions.live.json`, `orders.live.json`, ...)
touch live trading. For any Grok-delegated change that lands in those areas:

1. **`delegate` runs write-capable by default.** It edits files unless the request explicitly asks for read-only
   (`--read`, or "review only, do not change files"). For the areas above, either ask for read-only and apply the
   change yourself, or make sure Grok is working on a dedicated branch — never on `main` or the running config.
2. One topic per branch (`fix/…`, `feat/…`, `perf/…`, `chore/…`). Keeps each change reviewable and revertable on its own.
3. Review the diff yourself before it lands.
4. Run the test suite before accepting: `./scripts/run_unit_tests.sh --parallel` (preferred — pins Mongo to local,
   isolates the ledgers per test, ~10 s on 18 cores; without `--parallel` ~40 s) or `pytest` with the config in
   `pytest.ini`. Unit tests cannot reach the network (#324): a test that needs Gate/CMC/Grok data mocks the fetcher it
   imports (`tests/support/offline.py` has stubs), it never calls out.
5. Prefer running new/changed logic against `*.demo.json` or paper/backtest paths first, not the live files.
6. If a change affects order sizing, risk limits, or execution logic, treat Grok's output as a draft to audit line by
   line, not as ready-to-merge.

Lower-stakes areas (backtesting, data fetching, notifications, docs, tests, tooling) can be delegated more freely.

### Tests are the spec

7. **Existing test assertions are frozen.** A delegated change may add tests freely, but may change an existing
   assertion only when the ticket explicitly names the behaviour that changes and why. A red test is *reported*
   (which test, what it asserted, what it got) — never "fixed" to match new behaviour. `tests/unit/test_dca_stop_loss.py`
   is the cautionary example: the DCA stop-loss grace period looked like a bug and is tested intended behaviour.
8. Test diffs are reviewed with the same rigour as code diffs. A changed assertion is a changed contract.
9. Two suites may run concurrently **only** with distinct `PYTEST_DB_SUFFIX` values — it isolates both the Mongo
   test DB (#298) and the Redis key prefix (#319/#323); xdist workers add `_gwN` themselves (#321). Without a suffix
   both sessions share `xagent_pytest` and `pytest:default:*`, and results become order-dependent. Never run a suite
   from two worktrees with the same suffix.
10. Tests must not write into `data/` (#325, #327). Ledger files (orders, positions, trade history) are redirected per
   test by `isolate_demo_ledger_files`; a new test that touches another data file patches its path to `tmp_path`.
   Check with `touch marker; ./scripts/run_unit_tests.sh --parallel; find data -newer marker -type f`.

### Deploy branches are PR-only — never push to `staging` or `main`

`staging` auto-deploys to Railway on every push; `main` is the release branch. Neither ever receives a direct push
from any session, worktree, or Grok run:

- **Local guard:** `.git/hooks/pre-push` (shared by every worktree) rejects pushes to `staging`/`main`. Override only
  deliberately with `ALLOW_DEPLOY_PUSH=1` — and only for a reviewed, agreed deploy.
- **Remote guard:** GitHub branch protection on both branches — PR required, no force-push, no deletion, enforced for
  admins too.
- **Integration target is `rebuild/*`.** Topic branches merge there; the reviewed integration branch goes to `staging`
  as one PR when the phase is complete, at a moment the operator chooses.
- **PR sidebar (2026-09-21):** issue is the Kanban card. Every `gh pr create` copies the issue's type + allowlisted `area:*` + `priority:p*`, sets `--assignee jholze`, `--add-project "Trading Bot Kanban"`, and puts `Fixes #<n>` in `--body-file`. Title `feat|fix|docs(<area>): … (#N)`.
- Do not `kanban-set-status` on a PR number; Sprint/Backlog/Epics views use `is:issue`. devops STOPs squash if labels, assignee, or `Fixes #` is missing.

### Merging to `staging` — Claude's standing authority (decided 2026-09-09)

For a PR that doesn't touch the currently-measured spot execution/cost/risk-decision path (security fixes,
startup-order/config changes, UI-only changes like a Telegram command's presentation, additive fields, anything
scoped to a currently-inactive code path — judged fresh per PR, never assumed), **Claude merges it to `staging`
itself once ready.** jholze does not want to be asked per PR; this is a standing policy, not a one-off, and holds
regardless of any active shadow/observation period. When genuinely unsure whether a PR touches that path (e.g. it
shares a file with live BUY/SELL execution), check more carefully before merging rather than defaulting either way.

The bar before clicking merge, every time, no exceptions:

1. Reviewer PASS, or Claude's own line-by-line diff review if the task had no separate reviewer.
2. Full unit suite green **both** `--parallel` and sequential, a unique `PYTEST_DB_SUFFIX`, `data/` untouched.
3. CI green, no unresolved conflicts.
4. A ledger/health reference snapshot captured immediately before merging.

After the Railway redeploy: a full post-deploy check — deployment status, `/health` (build commit, writer lease,
cycle age), a ledger diff against the pre-merge snapshot, and a log scan for tracebacks / `LedgerWriteFailed` / etc.
The moment any of that comes back dirty, open a revert PR immediately — that's not a request for permission, it's
the same rollback path used for #322/#340/#341. Report the outcome to jholze honestly either way, clean or not.

5. **Close the ticket in the same step — never a separate pass.** The moment a merge is verified clean post-deploy,
   close every GitHub issue that PR fixes, with a comment naming the merge commit and PR. If the fix or finding is
   real and substantive but has no existing issue yet — a vulnerability found while reviewing someone else's PR, a
   UI change built directly at jholze's request in chat, anything a security- or money-path audit would file a
   ticket for — file one now (title, body with Finding / Why it matters / What was done / Verification, correct
   labels), then close it the same way. Add closed issues to the "Trading Bot Kanban" project board if they aren't
   on it already: the board auto-sets Status=Done when an issue already on the board gets closed, but closing
   BEFORE adding to the board does not backfill Done — add to the project first, or close then verify (and fix) the
   Status field. A docs-only PR with no behavior change is the one exception: no ticket needed. This is jholze's
   explicit standing rule ("Merge = Close = Done in the same step") — found violated 2026-09-10 when four merged,
   deployed PRs in one sitting left two tickets sitting at Backlog and two more with no ticket at all until he
   asked "tickets ordentlich gepflegt?" — don't wait to be asked again.

This authority is Claude's alone in *this* workflow (Claude Code session + `grok-build`) — it does not extend to the
separate Omnigent multi-agent team (`agents/lead/` and its domain workers), which always stops at "PR opened, human
merges" by its own, unrelated design.

Known friction: Claude Code's own auto-mode permission classifier may still block `gh pr merge` (and, notably, also
blocks Claude from adding a settings.json rule to pre-authorize it — by design, an agent can't grant itself a bypass
even under in-conversation authorization). If that happens, jholze adds the allow rule himself; Claude cannot do this
step.

### How to delegate a task

1. In Claude Code, describe the task normally — plan it out, confirm scope.
2. Create the branch for the topic first.
3. Hand it to Grok: `/grok-build:delegate "<precise task description, including files/modules involved>"`
4. Review the returned diff, run the tests, and integrate manually if anything needs adjustment.
5. For read-only investigation or analysis instead of code changes, say so explicitly in the request — it is **not**
   the default.

Run `/grok-build:check` to verify the Grok CLI is authenticated and reachable before relying on this workflow.
