# Workflow — Trading bot

Status: 2026-11-01, written by Kay (Grok Bot lead). Operating rules are in [`AGENTS.md`](../AGENTS.md); this file
describes who does what and how a change travels from ticket to merge.

- Omnigent project: **Trading bot**, ID `adba8ee6896849cd82e22bec7a881691`
- Workspace (Jens' Mac): `/Users/jholze/Documents/scripts/trading_bot`
- Repo: `jholze/xagent-trading-bot`
- PR base branch: **`staging`** (paper-trading bot). `main` is the release / live branch: Jens only.

## Roles and models

| Role | Who / model | Does | Does not |
|------|-------------|------|----------|
| Lead | Kay (Jens' Grok Bot lead) | Writes and scopes tickets (English), assigns coder/reviewer/tester, keeps one worktree per task, reports to Jens | Merge, deploy, change live config |
| Coder | Grok 4.7 (`grok-4.7`) | Implements one ticket on one branch in one worktree, runs the unit suite locally, opens the PR against `staging` | Review own PR, touch secrets, push to `staging`/`main` |
| Reviewer (per PR) | DeepSeek (`deepseek-v4-pro`) | Line-by-line review of every PR: spec/AC coverage, frozen tests, money-path impact, evidence | Write the code under review |
| Final reviewer | Claude Opus (`claude-opus-5-5`) | Mandatory final review (real money). Always before anything goes live and for every PR touching a high-risk path | Merge |
| Tester | Separate role (not the coder) | Runs the tests / acceptance checks locally and records the result as evidence | Change existing assertions to make tests pass |
| Owner | Jens Holze | Approves money-path PRs (`owner-approved` label), merges, deploys, releases `staging` → `main` | — |

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
   Money path: `docs/evidence/<pr-or-issue>.md` with a before/after table; the `owner-approved` label is set by Jens only.
4. **CI green** — required check `unit` (aggregates `classify`, `pytest`, `review-gates`, `hypothesis-evidence`, `ruff`).
5. **Review** — DeepSeek per-PR review; Claude Opus final review (mandatory for high-risk paths and before going live).
   Tester confirms acceptance tests.
6. **Merge** — by Jens only, after green CI and his OK. Merging to `staging` deploys the paper bot (`xagent-test`).
7. **Release** — `staging` → `main` PR (`gh pr create --base main --head staging`, see `DOCUMENTATION.en.md` §17),
   opened and merged by Jens only.

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

Never work in the main checkout. Removing a worktree is Jens' decision.

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
`owner-approved` label (Jens) and `docs/evidence/<pr-or-issue>.md`.
