# Workflow — Trading bot

Status: 2026-11-04 (first version 2026-11-01), written by Kay (Grok Bot lead). Operating rules are in [`AGENTS.md`](../AGENTS.md); this file
describes who does what and how a change travels from ticket to merge.

- Omnigent project: **Trading bot**, ID `adba8ee6896849cd82e22bec7a881691`
- Workspace (Jens' Mac): `/Users/jholze/Documents/scripts/trading_bot`
- Repo: `jholze/xagent-trading-bot`
- Omnigent host: `1201aea5000540c8a96378432409ef7c` (Jens' Mac); Kay's bot account: `trading-bot` (sees this project only, see `AGENTS.md` "Omnigent access")
- PR base branch: **`staging`** (paper-trading bot). `main` is the release / live branch: Jens only.

## Roles and models

| Role | Who / model | Does | Does not |
|------|-------------|------|----------|
| Lead | Kay (Jens' Grok Bot lead) | Writes and scopes tickets (English), assigns coder/reviewer/tester, keeps one worktree per task, reports to Jens; may merge into `staging` and set `owner-approved` only for proven paper-only improvements (merge rule below) | Merge to `main`, merge PRs touching `aria_bot.py`/`execution/`/`risk/`/`*.live.json` or real money, deploy (other than the `staging` auto-deploy), change live config, keys, withdraw |
| Coder | Grok 4.7 (`grok-4.7`) | Implements one ticket on one branch in one worktree, runs the unit suite locally, opens the PR against `staging` | Review own PR, touch secrets, push to `staging`/`main` |
| Reviewer (per PR) | DeepSeek (`deepseek-v4-pro`) | Line-by-line review of every PR: spec/AC coverage, frozen tests, money-path impact, evidence | Write the code under review |
| Final reviewer | Claude Opus (`claude-opus-5-5`) | Mandatory final review (real money). Always before anything goes live and for every PR touching a high-risk path. Interim: until 2026-11-06 07:00 Europe/Berlin DeepSeek gives the final review; afterwards Opus re-reviews every high-risk PR merged in between | Merge |
| Tester | Separate role (not the coder) | Runs the tests / acceptance checks locally and records the result as evidence | Change existing assertions to make tests pass |
| Owner | Jens Holze | Merges to `main`, merges PRs on live paths (`aria_bot.py`, `execution/`, `risk/`, `*.live.json`) and anything with real money, every deploy (other than the `staging` auto-deploy), releases `staging` → `main`, keys, withdraw; may also set `owner-approved` | — |

Model-family rule: at least two different model families see every PR, and the author and the reviewer are never the
same family (Grok codes → DeepSeek and Claude review).

High-risk paths (final Claude Opus review required): `aria_bot.py`, `execution/`, `risk/`, `*.live.json`, and every
entry of `ci/money_paths.json` (`strategies/`, `services/gainer_signal/`, `services/dca_sniper/`, risk/cost/sizing keys
in `config.json`).

## Flow

1. **Ticket** — Lead writes a GitHub issue in English: problem, spec with `AC1…ACn`, acceptance tests, risk class
   (money path yes/no). Behavior changes follow `docs/domain/spec-writing.md`.
2. **Coder** — creates the branch and its worktree, implements, adds tests, runs the unit suite locally.
3. **PR** — against `staging`, English title, PR body with an `## Evidence` heading (test result, before/after metric).
   Money path: `docs/evidence/<pr-or-issue>.md` with a before/after table; the `owner-approved` label is set by Jens, or by Kay for staging merges.
4. **CI green** — required check `unit` (aggregates `classify`, `pytest`, `review-gates`, `hypothesis-evidence`, `ruff`).
5. **Review** — DeepSeek per-PR review in an Omnigent session of this project (agent `deepseek_review`, title
   `Kay review PR<n> deepseek`, one session per PR and model, reused on re-review). Claude Opus final review (agent
   `reviewer`, title `Kay review PR<n> opus`), mandatory for high-risk paths and before going live; interim DeepSeek
   final review until 2026-11-06 07:00 Europe/Berlin. Each review is posted verbatim as a PR comment naming the model
   and the reviewed head. Real findings are fixed on the branch. Tester confirms acceptance tests.
6. **Merge** — Merge rule (Jens, 2026-11-04; replaces the 2026-11-01 rule): Kay squash-merges into `staging` only a
   proven improvement with evidence in the PR (tests, backtest or paper trading), paper trading only, review gate
   passed, CI `unit` green, branch rebased on `staging`. Documentation-only PRs may be merged after the review gate.
   PRs touching `aria_bot.py`, `execution/`, `risk/`, `*.live.json`, or anything with real money: Kay opens the PR and
   stops; Jens merges. Merging to `staging` deploys the paper bot (`xagent-test`, Railway environment `test`); Kay
   verifies the deployment read-only (status SUCCESS, no new tracebacks) and logs it.
7. **Release** — `staging` → `main` PR (`gh pr create --base main --head staging`, see `DOCUMENTATION.en.md` §17),
   opened and merged by Jens only.

How code reaches `main`: only through the `staging` → `main` release PR, opened and merged by Jens. No topic branch
targets `main`.

Back channel: the lead reports to Jens' assistant in a session of the Omnigent project above.

## Branch naming

Prefixes in use in this repo: `fix/`, `feat/`, `docs/`, `chore/`, `perf/`, `test/`, `ci/`, `hotfix/`, `cursor/`
(Cursor cloud agents). Use `<type>/<short-slug>-<issue>` in lowercase kebab case, for example
`fix/dca-trail-new-peak-558` or `docs/agents-md`. Retired: `rebuild/*` as integration branch; `feature/*` (old docs)
— use `feat/`.

## One worktree per task

On Jens' Mac every task gets its own worktree:

```bash
cd /Users/jholze/Documents/scripts/trading_bot
git fetch origin staging
git worktree add -b <type>/<slug> /Users/jholze/Documents/scripts/trading_bot-worktrees/<branch-slug> origin/staging
# <branch-slug> = branch name with "/" replaced by "-", e.g. fix-dca-trail-new-peak-558
```

Never work in the main checkout. Removing a worktree is Jens' decision. Kay's own worktrees on the Grok Bot box live
under `/workspace/wt-<slug>`; they are scratch copies of the same branches and never replace the PR.

## Test commands

| What | Command | When |
|------|---------|------|
| Install | `pip install -r requirements.txt` (Python 3.13) | once per environment |
| Unit suite (as CI) | `UNIT_TEST_PROGRESS=0 PYTEST_DB_SUFFIX=<unique> ./scripts/run_unit_tests.sh -q` | before every PR |
| Unit suite, fast | `PYTEST_DB_SUFFIX=<unique> ./scripts/run_unit_tests.sh --parallel` | during development |
| Data isolation check | `touch marker; ./scripts/run_unit_tests.sh --parallel; find data -newer marker -type f` (must print nothing) | changes that touch ledgers/data |
| Lint (as CI) | `pip install 'ruff==0.17.3'` then `ruff check --select E9,F821,F823 <changed .py files>` (CI: `scripts/ci/ruff_changed.py`) | before every PR with Python changes |
| Review gates (as CI) | `REVIEW_GATE_BASE=<base sha> REVIEW_GATE_HEAD=<head sha> python scripts/ci/review_gates.py` | money-path or evidence questions |
| Evidence JSON (as CI) | `python -m tools.hypothesis_protocol --evidence docs/evidence` | when adding `docs/evidence/*.json` |
| Backtest gate | `python -m backtest.harness --data <export dir> --candles <candle cache> [--fetch] --scenarios a,b,c,d` (writes `docs/backtests/<stem>.md`) | strategy / money-path changes (before/after evidence) |
| Telegram E2E (optional) | `TELEGRAM_E2E=1 pytest -m telegram_e2e tests/e2e_telegram` — needs Telethon secrets, local/demo bot only | not a PR gate |

Local services bind to `127.0.0.1` (Mongo 27017, Redis 6379, bot 5000 with `HOST=127.0.0.1`, see
`docs/LOCAL_STACK.md`). Tests use local Mongo only (`xagent_pytest[_<suffix>]`), never Railway.

What must be green before a PR: the unit suite locally; in CI the required check `unit`. The `review-gates` job needs an
`Evidence` heading (levels 1–3) in the PR body or a `docs/evidence/*.md` file; money-path PRs additionally need the
`owner-approved` label (Jens, or Kay for staging merges) and `docs/evidence/<pr-or-issue>.md`.
