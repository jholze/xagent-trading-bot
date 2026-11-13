# Workflow — Trading bot

Status: 2026-11-05 (first version 2026-11-01), written by Kay (Grok Bot lead). Operating rules are in [`AGENTS.md`](../AGENTS.md); this file
describes who does what and how a change travels from ticket to merge.

- Omnigent project: **Trading bot**, ID `adba8ee6896849cd82e22bec7a881691`
- Active Omnigent workspace (Jens' Mac): `/Users/jholze/Documents/scripts/trading_bot_omnigent`
- Task worktrees: `/Users/jholze/Documents/scripts/trading_bot_omnigent/.worktrees/<slug>`
- Repo: `jholze/xagent-trading-bot`
- Omnigent host: Jens' Mac (host id from Jens' instruction or `list_hosts`); Kay's bot account: `trading-bot` (sees this project only, see `AGENTS.md` "Omnigent access")
- PR base branch: **`staging`** (paper-trading bot). `main` is the release / live branch: Jens only.

## Entry gate (Hindsight + Kay)

Before research, analysis, a ticket, or an Omnigent session, follow [`AGENTS.md`](../AGENTS.md) rule 0:

- Recall Hindsight bank `coding-agent::trading_bot` on the Mac at `http://127.0.0.1:8888`. Omnigent MCP is not the recall path.
- Cite source refs `memory:<id>` / `doc:<id>`.
- If the recall fails, write `RECALL_FAILED`. A silent skip is a process violation.
- Kay sessions and tickets are prepared via the ops gate (`kay-entry`, or the equivalent Pflichtblock + Hindsight). Raw MCP create without Pflichtblock + Hindsight is a process violation.

Closeout into Hindsight: evidence-backed lessons only. Retain only when the Hindsight LLM capability is ok. A pending queue is allowed. No bulk import. Reflect is not the default.

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
in `config.json`, plus the patterns `aria_bot.py` and `*.live.json`).

## Flow

1. **Ticket** — Entry gate first (section above). Lead writes a GitHub issue in English: problem, spec with `AC1…ACn`, acceptance tests, risk class
   (money path yes/no), and **Existing in memory (Hindsight)** (`memory:<id>` / `doc:<id>`, or `RECALL_FAILED`). Behavior changes follow `docs/domain/spec-writing.md`.
2. **Coder** — creates the branch and its worktree, implements, adds tests, runs the unit suite locally.
3. **PR** — against `staging`, English title, PR body with an `## Evidence` heading (test result, before/after metric).
   Money path: `docs/evidence/<pr-or-issue>.md` with a before/after table; the `owner-approved` label is set by Jens, or by Kay for staging merges.
4. **CI green** — required check `unit` (aggregates `classify`, `pytest`, `review-gates`, `hypothesis-evidence`, `ruff`).
5. **Review** — DeepSeek per-PR review in an Omnigent session of this project (agent `deepseek_review`, verified with
   `list_agents` on 2026-11-04; confirm the session's model from its usage record; title
   `Kay review PR<n> deepseek`, one session per PR and model, reused on re-review). Claude Opus final review (agent
   `reviewer`, verified the same way, title `Kay review PR<n> opus`), mandatory for high-risk paths and before going live; interim DeepSeek
   final review until 2026-11-06 07:00 Europe/Berlin. Each review is posted verbatim as a PR comment naming the model
   and the reviewed head. Real findings are fixed on the branch. Tester confirms acceptance tests.
6. **Merge** — Merge rule (Jens, 2026-11-04; replaces the 2026-11-01 rule): Kay squash-merges into `staging` only a
   proven improvement with evidence in the PR (tests, backtest or paper trading), paper trading only, review gate
   passed, CI `unit` green, branch rebased on `staging`. Documentation-only PRs may be merged after the review gate.
   PRs touching `aria_bot.py`, `execution/`, `risk/`, `*.live.json`, or anything with real money: Kay opens the PR and
   stops; Jens merges. Merging to `staging` deploys the paper bot (`xagent-test`, Railway environment `test`); Kay
   verifies the deployment read-only (status SUCCESS, no new tracebacks) and logs it.
7. **Release** — a `staging` → `main` pull request, opened and merged by Jens only.
   The first five such releases after 8 November 2026 are with Jens. Naming, the blocked version gate, and the
   board views are [`docs/release-framework.md`](release-framework.md) and [`docs/releases/2026-11-plan.md`](releases/2026-11-plan.md).
   `DOCUMENTATION.en.md` §17 is a retired checklist. Do not run it. At the SHAs recorded on 8 November 2026, `main` is
   963 commits behind `staging`; acceptance reviews that whole range. Nothing merges it automatically.
   A documentation-only pull request may be merged after the review gate under the rule above, and that merge
   auto-deploys paper. When the owner forbids the deploy, the pull request stays open. This release-policy change
   stays open.

How code reaches `main`: only through the `staging` → `main` release PR, opened and merged by Jens. No topic branch
targets `main`.

## Where a procedure lives

Rules: [`AGENTS.md`](../AGENTS.md). Release decisions: [`docs/release-framework.md`](release-framework.md). Tickets and the board hold scope and status; this file does not write them. Skills [`docs/skills/release-planning/SKILL.md`](skills/release-planning/SKILL.md) and [`docs/skills/ticket-spec/SKILL.md`](skills/ticket-spec/SKILL.md) point at those rules and do not replace them. The scheduler that fires a job is the trigger; [`docs/operations/routines.md`](operations/routines.md) only maps it. Ticket shape: [`docs/domain/spec-writing.md`](domain/spec-writing.md) and [`docs/requirements-engineering-guide.md`](requirements-engineering-guide.md) §1a item 7, §4, and §5. A raw request is not Ready. Manual dry run: [`docs/releases/2026-11-08-intake-tabletop.md`](releases/2026-11-08-intake-tabletop.md).

`scripts/kanban_status.py` still moves Status when it sees `omnigent:ready`, `review:lena`, and `review:viktor`. Those labels are retired for humans. The script is unchanged. It does not set Done, it retains a Done it already finds, a freeze comment can move Backlog to Spec/Acceptance, and a merged staging soak pull request can move a card to Shadow before the parked check. Views on the board do not clear that debt. CI `unit` does not use those labels. Intake is not enforced by CI.

Back channel: the lead reports to Jens' assistant in a session of the Omnigent project above.

## Branch naming

Prefixes in use in this repo: `fix/`, `feat/`, `docs/`, `chore/`, `perf/`, `test/`, `ci/`, `hotfix/`, `cursor/`
(Cursor cloud agents). Use `<type>/<short-slug>-<issue>` in lowercase kebab case, for example
`fix/dca-trail-new-peak-558` or `docs/agents-md`. Retired: `rebuild/*` as integration branch; `feature/*` (old docs)
— use `feat/`.

## One worktree per task

On Jens' Mac every task gets its own worktree:

```bash
cd /Users/jholze/Documents/scripts/trading_bot_omnigent
git fetch origin staging
git worktree add -b <type>/<slug> /Users/jholze/Documents/scripts/trading_bot_omnigent/.worktrees/<slug> origin/staging
# <slug> = branch name with "/" replaced by "-", e.g. fix-dca-trail-new-peak-558
```

The active Omnigent workspace is that clone. Pass the clone, or the task worktree under `.worktrees/<slug>`, on `create_session`. Do not rewrite historical sessions. Older Mac task worktrees under `/Users/jholze/Documents/scripts/trading_bot-worktrees/` are historical inventory only (`docs/agent-onboarding.md` open question 15). New worktrees are not created there.

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
