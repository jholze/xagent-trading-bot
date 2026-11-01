# Agent operating rules

This file is the single source of truth for every coding agent (Grok, DeepSeek, Claude, Cursor, Codex, ...) working in this repository. `CLAUDE.md` only points here.

The owner is Jens Holze. Only Jens merges to main. Kay (lead agent) may merge PRs into staging (paper) when CI `unit` is green, an evidence file exists, the change is a measured improvement, and money-path PRs carry `owner-approved` set per the release framework. Decided by Jens on 2026-11-01.

> **Staging vs. live.** Branch `staging` is the **paper-trading** bot: merging to `staging` auto-deploys the paper bot on Railway (`xagent-test`). Anything that reaches `main` or touches live trading (real money, live Gate account, `*.live.json`) is **Jens only**. Open pull requests against `staging` only. Never push to `main` or `staging`; only Jens merges to `main`; Kay (lead agent) may merge into `staging` under rule 6 below. Never enable live trading. Never add API keys, exchange secrets, or credentials.

## Safety guardrail — this bot trades with real state

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

The full money-path list used by CI is `ci/money_paths.json` (see "Money path" below). It adds `strategies/`,
`services/gainer_signal/`, `services/dca_sniper/`, and the risk/cost/sizing keys of `config.json` to the areas above.

## Mandatory rules

These rules are fixed by Jens (2026-11-01). They override anything else in this repository.

1. **Model rule** (not a Grok-only setup):
   - Coder: **Grok 4.7** (`grok-4.7`).
   - Per-PR reviewer: **DeepSeek** (`deepseek-v4-pro`).
   - Final review: **Claude Opus** (`claude-opus-5-5`). Mandatory for this project because it handles real money, and always before anything goes live or a PR touches a high-risk path (safety guardrail list above or `ci/money_paths.json`).
   - Tester is a separate role from coder and reviewers.
   - At least two different model families see every PR. Author and reviewer are never the same model family.
2. Tickets, issues, and pull-request titles are always in **English**.
3. Development and tests run **locally only**. Bind local services to `127.0.0.1` (for the bot: `HOST=127.0.0.1`; its default is `0.0.0.0`).
4. This project is **production** (real money, live trading state). No deploy (Railway or any other target), no live config changes, no orders, no account or paid-service creation. Do not run `scripts/deploy_staging.sh`, `scripts/deploy_test_branch.sh`, `scripts/railway_trigger_test_deploy.sh`, or anything that writes to Railway or a Railway Mongo.
5. **Never read or print secrets**: `.env`, `.env.local`, `ngrok.log`, credentials files, tokens (also `.cursor/mcp.json` and similar local tool configs).
6. **Merge rule.** Only Jens merges to main. Kay (lead agent) may merge PRs into staging (paper) when CI `unit` is green, an evidence file exists, the change is a measured improvement, and money-path PRs carry `owner-approved` set per the release framework. Decided by Jens on 2026-11-01.

Workflow, roles, branch naming, and test commands: [`docs/workflow.md`](docs/workflow.md). Project facts for new agents: [`docs/agent-onboarding.md`](docs/agent-onboarding.md).

## Deploy branches are PR-only — never push to `staging` or `main`

`staging` auto-deploys the paper bot to Railway on every push; `main` is the release branch. Neither ever receives a
direct push from any session, worktree, or agent run:

- **Local guard:** `.git/hooks/pre-push` (shared by every worktree on Jens' Mac) rejects pushes to `staging`/`main`.
  Agents never use the `ALLOW_DEPLOY_PUSH=1` override.
- **Remote guard:** GitHub branch protection on both branches — PR required, no force-push, no deletion.
- **Integration target is `staging`.** Topic branches open their PR against `staging`. The release PR
  `staging` → `main` is opened and merged by Jens only.

## Delegation policy

Superseded by the model rule above: Grok writes the code, DeepSeek reviews each PR, Claude Opus gives the final
review, a separate tester verifies. The `grok-build` plugin (`/plugin install grok-build@xai-grok-build`) is the
tooling a Claude session uses to hand coding tasks to the Grok CLI:

| Command | Use |
|---------|-----|
| `/grok-build:check` | Verify the Grok CLI is installed, authenticated and reachable |
| `/grok-build:delegate "<task>"` | Hand a coding task to Grok — **edits files by default** |
| `/grok-build:review` | Grok code review against local git state |
| `/grok-build:critique` | Grok challenges the implementation approach and design choices |
| `/grok-build:runs` | List active and recent runs for this repo |
| `/grok-build:show [run-id]` | Show the stored final output of a finished run |
| `/grok-build:stop [run-id]` | Stop an active background run |

`delegate` flags: `--background` / `--wait`, `--resume` / `--fresh`, `--model <model>`, `--effort <low|medium|high>`.
Only hand Grok tasks once they are concrete enough to describe in a paragraph; keep the spec (what the strategy
should do) with the lead and the owner.

## Spec before code

Every behavior change starts with a written spec: numbered acceptance criteria `AC1`…`ACn` and the acceptance tests that pin them. Freeze the spec before building. A later change is `Rev N` and does not rewrite the previous revision in place. Standard: `docs/domain/spec-writing.md`.

## Tests are the spec

Existing test assertions are frozen. Add tests freely. Change an existing assertion only when the spec names the behavior that changes and why. Report a red test (which test, what it asserted, what it got). Do not edit it to match new behavior. A test diff is a contract change and is reviewed with the same rigour as a code diff. `tests/unit/test_dca_stop_loss.py` is the cautionary example: the DCA stop-loss grace period looked like a bug and is tested intended behaviour.

## Evidence

Every change must be measurably better. Put a before/after metric on the pull request: tests run, mutants killed, backtest values after fees, risk metrics, or paper results. The required check is still the job named `unit`. Pytest may be skipped for an allowlisted documentation diff. The evidence gate, hypothesis JSON check, and changed-file Ruff still run, and `unit` fails if they fail (`scripts/ci/review_gates.py`, rules in `ci/money_paths.json`). A non-money-path PR needs `docs/evidence/*.md` or an `Evidence` heading (levels 1–3) in the PR body.

## Money path

Entries, exits, stops, DCA, sizing, risk limits, config limits, and locks are the money path. The path list is `ci/money_paths.json` and is the list the gate uses. A money-path pull request fails `unit` unless it has the label `owner-approved` and a file `docs/evidence/<pr-or-issue>.md` with a before/after table. The owner decides. Do not add `owner-approved` yourself. A money-path PR also needs the Claude Opus final review (model rule).

## Symbols

No hardcoded coin names in strategy or risk code. Symbol and source rules live in config. Tests use fixture configs.

## Test isolation

- Run units with `./scripts/run_unit_tests.sh`. `--parallel` pins local Mongo and isolates ledgers per test.
- Two suites at once need distinct `PYTEST_DB_SUFFIX` values (isolates the Mongo test DB and the Redis key prefix). xdist workers append `_gwN`. The same suffix from two worktrees shares `xagent_pytest` and makes results order-dependent.
- Tests must not write `data/`. Ledger files are redirected per test (`isolate_demo_ledger_files`). A new test that touches another data file patches that path to `tmp_path`. Check: `touch marker; ./scripts/run_unit_tests.sh --parallel; find data -newer marker -type f`.
- Unit tests cannot reach the network. Mock the fetcher (`tests/support/offline.py`).

## Domain

Review knowledge that used to live with people is in `docs/domain/`. Use the numbers written there. Values marked open or unknown are not decided; do not fill them in.

## What this file replaces

`CLAUDE.md` is now a one-line pointer to this file. The following instructions from older `CLAUDE.md` versions are **retired** (as of 2026-10-31) — do not follow them:

- Claude plans and reviews; Grok (`grok-build`) writes the code (replaced by the model rule above).
- Claude merges non-money-path pull requests to `staging` without asking, then closes tickets and checks the deploy.
- Omnigent labels (`omnigent:ready`, `review:*`, `veto:*`) and a Ready-to-Work gate before coding. Do not set Omnigent labels.
- `rebuild/*` as the integration branch, with `staging` only at the end of a phase.

Background, not operating rules: `README.md`, `docs/umbau/README.md`, and `DOCUMENTATION.md` are project background. The spec-folder shape is `docs/requirements-engineering-guide.md` (section 1a is aligned with this file; the Kay / Lena / Viktor / Omnigent chain in older copies of that section is retired). Domain thresholds and review gates are `docs/domain/`.
