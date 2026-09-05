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
4. Run the test suite before accepting: `./scripts/run_unit_tests.sh` (preferred — pins Mongo to local, isolates demo
   ledgers) or `pytest` with the config in `pytest.ini`.
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
   test DB (#298) and the OHLCV Redis key prefix (#319). Without a suffix both sessions share `xagent_pytest` and
   `pytest:default:ohlcv:*`, and results become order-dependent. Never run a suite from two worktrees with the same
   suffix.

### Deploy branches are PR-only — never push to `staging` or `main`

`staging` auto-deploys to Railway on every push; `main` is the release branch. Neither ever receives a direct push
from any session, worktree, or Grok run:

- **Local guard:** `.git/hooks/pre-push` (shared by every worktree) rejects pushes to `staging`/`main`. Override only
  deliberately with `ALLOW_DEPLOY_PUSH=1` — and only for a reviewed, agreed deploy.
- **Remote guard:** GitHub branch protection on both branches — PR required, no force-push, no deletion, enforced for
  admins too.
- **Integration target is `rebuild/*`.** Topic branches merge there; the reviewed integration branch goes to `staging`
  as one PR when the phase is complete, at a moment the operator chooses.

### How to delegate a task

1. In Claude Code, describe the task normally — plan it out, confirm scope.
2. Create the branch for the topic first.
3. Hand it to Grok: `/grok-build:delegate "<precise task description, including files/modules involved>"`
4. Review the returned diff, run the tests, and integrate manually if anything needs adjustment.
5. For read-only investigation or analysis instead of code changes, say so explicitly in the request — it is **not**
   the default.

Run `/grok-build:check` to verify the Grok CLI is authenticated and reachable before relying on this workflow.
