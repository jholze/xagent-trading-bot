# Agent operating rules

This file is the single source of truth for every coding agent (Grok, DeepSeek, Claude, Cursor, Codex, ...) working in this repository. `CLAUDE.md` only points here.

The owner is Jens Holze. Only Jens merges to `main`. Kay may merge into `staging` only under mandatory rule 6 (proven paper-only improvements; live paths and real money stay with Jens).

> **Staging vs. live.** Branch `staging` is the **paper-trading** bot: merging to `staging` auto-deploys the paper bot on Railway (`xagent-test`). Anything that reaches `main` or touches live trading (real money, live Gate account, `*.live.json`) is **Jens only**. Open pull requests against `staging` only. Never push to `main` or `staging`. Only Jens merges to `main`; Kay may merge into `staging` under rule 6 below. Never enable live trading. Never add API keys, exchange secrets, or credentials.

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

These rules are fixed by Jens (2026-11-01, updated 2026-11-04 and 2026-11-05). They override anything else in this repository.

0. **Docs map + Hindsight (before research / analysis / tickets).** Before any research, analysis, ticket/issue writing, or PR planning in this repository, agents must:
   1. Read this file (`AGENTS.md`) and the docs map below for the topic.
   2. Open the relevant linked docs (at least the settings/features reference when the work touches config, flags, Telegram commands, or options).
   3. Query **Hindsight** memory for bank `coding-agent::trading_bot` (knowledge-page search and/or reflect). Omnigent MCP for bot account `trading-bot` does **not** expose Hindsight tools — use the Hindsight coding-agent tools or the local Hindsight HTTP API on Jens' Mac. Prefer read-only; writes need an explicit retain/confirm path.
   4. If docs and Hindsight disagree, **trust git + `AGENTS.md` + committed `config.json`**, note the contradiction in the ticket/PR, and do not silently prefer stale memory.

   Docs map:

   | Topic | Document |
   |-------|----------|
   | Operating rules (this file) | `AGENTS.md` |
   | Workflow / roles / evidence | `docs/workflow.md` |
   | Agent onboarding facts | `docs/agent-onboarding.md` |
   | Settings / features / options / Telegram commands | `docs/reference/settings-features-options.md` (+ `.generated.md`) |
   | Agent docs index (llms.txt) | `docs/llms.txt` |
   | Human product docs | `README.md`, `DOCUMENTATION.md` |
   | Domain thresholds | `docs/domain/` |
   | Docs audit 2026-11-05 | `docs/audit/doku_audit_2026-11-05.md` |

1. **Model rule** (not a Grok-only setup):
   - Coder: **Grok 4.7** (`grok-4.7`).
   - Per-PR reviewer: **DeepSeek** (`deepseek-v4-pro`).
   - Final review: **Claude Opus** (`claude-opus-5-5`). Mandatory for this project because it handles real money, and always before anything goes live or a PR touches a high-risk path (safety guardrail list above or `ci/money_paths.json`).
   - Tester is a separate role from coder and reviewers.
   - At least two different model families see every PR. Author and reviewer are never the same model family.
   - Every PR states which model reviewed it: one PR comment per review with the model id (confirmed from the Omnigent session's usage record) and the reviewed head commit.
   - Interim rule (Jens, 2026-11-04): Claude is rate-limited until **2026-11-06 07:00 Europe/Berlin**. Until then DeepSeek also gives the final review. Afterwards Claude Opus reviews every PR merged in the meantime that touches a high-risk path; the catch-up list is kept by Kay (`/workspace/umbau/opus_nachholen.md` on Kay's Grok Bot box; each affected PR also carries a review comment saying Opus is still due) and findings become fix PRs against `staging`.
2. Tickets, issues, and pull-request titles are always in **English**.
3. Development and tests run **locally only**. Bind local services to `127.0.0.1` (for the bot: `HOST=127.0.0.1`; its default is `0.0.0.0`).
4. This project is **production** (real money, live trading state). No deploy (Railway or any other target), no live config changes, no orders, no account or paid-service creation. Do not run `scripts/deploy_staging.sh`, `scripts/deploy_test_branch.sh`, `scripts/railway_trigger_test_deploy.sh`, `railway variables`, or anything that writes to Railway or a Railway Mongo. Railway status, deployments, and logs may be read (read-only) to verify the `staging` auto-deploy; search logs for specific patterns and never copy log lines that contain secrets, tokens, or connection strings into any output.
5. **Never read or print secrets**: `.env`, `.env.local`, `ngrok.log`, credentials files, tokens (also `.cursor/mcp.json` and similar local tool configs). The only exception is the Omnigent bot token described under "Omnigent access" below, used exactly as described there.
6. **Merges** (Jens, 2026-11-04; replaces the 2026-11-01 22:57 rule). Kay may squash-merge a PR into `staging` himself only when **all** of these hold:
   - it is a proven improvement with evidence in the PR (tests, backtest, or paper-trading results; see "Evidence"),
   - it affects paper trading only,
   - the review gate passed (DeepSeek review on every PR; final review per rule 1),
   - CI `unit` is green and the branch is up to date with `staging` (rebased).

   Jens only: `main` (every merge to `main`, including the `staging` → `main` release), every deploy other than the `staging` auto-deploy that a permitted staging merge triggers, the live paths `aria_bot.py`, `execution/`, `risk/`, `*.live.json`, and anything else with real money. For those Kay opens the PR and stops. Kay may set the `owner-approved` label only for a staging merge he is allowed to do under this rule. Documentation-only PRs may be merged to `staging` after the review gate.

Workflow, roles, branch naming, and test commands: [`docs/workflow.md`](docs/workflow.md). Project facts for new agents: [`docs/agent-onboarding.md`](docs/agent-onboarding.md).

## Omnigent access (bot account `trading-bot`)

- Kay works in Omnigent through his own bot account, handle `trading-bot`. It sees only the project **Trading bot** (ID `adba8ee6896849cd82e22bec7a881691`); any other project must be rejected. Check this with `list_projects` before the first use.
- Token: in the bot's credentials file on Jens' Mac (JSON field `token`, file mode 0600; Jens gives Kay the location, it is not repeated in this public repo). Send it as `Authorization: Bearer <token>`. Read it inside the same command that uses it (for example into a Python variable). Never print, echo, log, or write the token or `webhook_secret` to any file, ticket, PR, commit, or chat; no `set -x`.
- Endpoint: `http://127.0.0.1:6767/mcp` when running on the Mac; otherwise the public MCP URL Jens provides. The token is valid only for `/mcp` and `/v1/chat/*`; other paths return 401, which is expected.
- `create_session` always passes `project_id`, the Mac's `host_id` (from Jens' instruction or `list_hosts`), and the workspace `/Users/jholze/Documents/scripts/trading_bot` (or the task worktree).
- Review sessions: title `Kay review PR<n> <model>` (for example `Kay review PR770 deepseek`). Never create a second review session for the same PR and model; reuse the existing one (`list_sessions` with the project id first).
- Feedback to Jens' assistant goes into a session of this project.

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

Entries, exits, stops, DCA, sizing, risk limits, config limits, and locks are the money path. The path list is `ci/money_paths.json` and is the list the gate uses. A money-path pull request fails `unit` unless it has the label `owner-approved` and a file `docs/evidence/<pr-or-issue>.md` with a before/after table. For staging merges the label is set by Jens or by Kay (only for merges rule 6 allows him); no other agent adds `owner-approved`. A money-path PR also needs the Claude Opus final review (model rule, interim rule included). A PR touching `aria_bot.py`, `execution/`, `risk/`, or `*.live.json` is opened and left for Jens (rule 6).

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

`CLAUDE.md` is now only a pointer to this file. The following instructions from older `CLAUDE.md` versions are **retired** (as of 2026-10-31) — do not follow them:

- Claude plans and reviews; Grok (`grok-build`) writes the code (replaced by the model rule above).
- Claude merges non-money-path pull requests to `staging` without asking, then closes tickets and checks the deploy.
- Omnigent labels (`omnigent:ready`, `review:*`, `veto:*`) and a Ready-to-Work gate before coding. Do not set Omnigent labels.
- `rebuild/*` as the integration branch, with `staging` only at the end of a phase.

Background, not operating rules: `README.md`, `docs/umbau/README.md`, and `DOCUMENTATION.md` are project background. The spec-folder shape is `docs/requirements-engineering-guide.md` (section 1a is aligned with this file; the Kay / Lena / Viktor / Omnigent chain in older copies of that section is retired). Domain thresholds and review gates are `docs/domain/`.
