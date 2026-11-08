# Agent Onboarding – Trading bot (xagent-trading-bot)

<!-- Template v0.1 (2026-11-01). Filled in by the project bot, saved as docs/agent-onboarding.md via PR.
Answer from the repo and what you know. Never guess: write OPEN and add it to "Open questions".
Never copy secrets, tokens, .env contents or credentials into this file. -->

- Omnigent project ID: `adba8ee6896849cd82e22bec7a881691` ("Trading bot")
- Active Omnigent workspace: `/Users/jholze/Documents/scripts/trading_bot_omnigent`
- Task worktrees: `/Users/jholze/Documents/scripts/trading_bot_omnigent/.worktrees/<slug>`
- Hindsight recall (before research, analysis, ticket, or session): bank `coding-agent::trading_bot` via Mac `http://127.0.0.1:8888`. Omnigent MCP is not the recall path. Source refs `memory:<id>` / `doc:<id>`. On failure write `RECALL_FAILED` (no silent skip). Rule: `AGENTS.md` rule 0.
- Kay sessions and tickets: ops gate `kay-entry` (or the equivalent Pflichtblock + Hindsight). Raw MCP create without Pflichtblock + Hindsight is a process violation.
- Hindsight closeout: evidence-backed lessons only; retain only when the Hindsight LLM capability is ok; a pending queue is allowed; no bulk import; reflect is not the default.
- Repo: `jholze/xagent-trading-bot`, default branch: `main`; integration / PR base branch: `staging`
- Bot account handle: `trading-bot` (scope: this project only; access check 2026-11-04: `list_projects` shows only "Trading bot", a foreign project id returns `not_found`)
- Omnigent host for sessions: Jens' Mac (host id from Jens' instruction or `list_hosts`, not repeated in this public repo)
- Phase: production (real money, live trading state); `staging` = paper-trading bot
- Filled in by / date: Kay (Grok Bot lead), 2026-11-01, updated 2026-11-04 and 2026-11-05

## 1. Purpose

Autonomous crypto trading agent for Gate (spot, via `ccxt`): technical analysis (RSI, Bollinger, volume, ATR),
X/Twitter signals and CMC sentiment are combined into buy/sell decisions with risk limits, cooldowns, DCA, exit ladder
and trailing stops, controlled and explained through Telegram (`README.en.md`).
Key metric of success: OPEN (proposed: net PnL after fees and slippage versus a buy-and-hold baseline at bounded
drawdown, measured on paper first; see the evidence rules in `AGENTS.md` and `docs/domain/`).

## 2. Users

- Operator / owner: Jens Holze (Telegram commands, desk UI, merges and deploys).
- Tenants: the bot is multi-tenant (`core/tenant_context.py`, tenant configs; e.g. tenant "henry" in evaluations).
  Who the tenants are and whether they trade real money: OPEN.
- Agents (Grok Bot lead, coder, reviewers, tester) through PRs only.
- Sensitive data: exchange API keys, Telegram bot tokens, X/xAI/CMC API keys, Railway/Mongo URLs (all in `.env`,
  `.env.local`, Railway variables — never read), trade ledgers and positions (`*.live.json`, Mongo `xagent`/`xagent_test`),
  tenant configs and balances.

## 3. Stack

- Language/runtime: Python 3.13 (CI and Docker image `python:3.13-slim-bookworm`); package manager `pip` with
  `requirements.txt` (+ optional `requirements-rag.txt`).
- Main libraries: Flask (webhook/health), ccxt 4.5.88 (Gate), pandas, TA-Lib, pymongo, redis, mcp, openai client
  (Grok/xAI), websocket-client, rich; tests with pytest, pytest-xdist, hypothesis.
- Databases: MongoDB 7 (ledger; `xagent_test` on staging, `xagent` production, `xagent_pytest*` for tests), Redis 7
  (bus/cache, optional — in-process fallback), Weaviate (memory stack, optional).
- Desk UI: `tools/desk` — React 19 + Vite + Tailwind 4 (Node 22, npm), baked into the Docker image.
- Backtest acceleration: `backtest/rust` (Cargo).
- Repo layout and layer boundaries:
  - `aria_bot.py` — main process (Flask webhook + price/signal cycle). High risk.
  - `execution/` — exchange adapters, order fetch, recovery. High risk.
  - `risk/` — risk manager, position capacity, slot eviction, cash policy. High risk.
  - `strategies/`, `services/gainer_signal/`, `services/dca_sniper/` — decision logic (money path).
  - `core/` — config, costs, tenant routing, execution mode, ledger helpers.
  - `services/`, `workers/`, `bus/`, `scanners/`, `intelligence/`, `hermes/` — runtime services, background work, memory.
  - `notifications/`, `telegram_notifier.py`, `locales/` — Telegram UI and texts.
  - `backtest/`, `experiments/`, `auswertungen/` — offline analysis.
  - `tests/` (`unit/`, `integration/`, `e2e_telegram/`, `universe/`, `fixtures/`, `support/`), `ci/`, `scripts/`, `deploy/`, `docs/`, `specs/`.

## 4. Run and deploy

- Install: `pip install -r requirements.txt`
- Dev (local, demo/paper): `bash scripts/start_demo_with_ngrok.sh`; local stack with Railway parity:
  `bash scripts/local_stack_up.sh`, `bash scripts/local_stack_health.sh --infra`, `bash scripts/local_stack_bot.sh`
  (`docs/LOCAL_STACK.md`). Agents never start a production stack (`scripts/start_stack.sh`, `start_with_ngrok.sh`).
- Lint: Ruff 0.17.3 with `E9,F821,F823` on changed Python files (`scripts/ci/ruff_changed.py`).
- Typecheck: none for Python (OPEN); desk: `tsc -b` inside `npm run build` in `tools/desk`.
- Test: `UNIT_TEST_PROGRESS=0 PYTEST_DB_SUFFIX=<unique> ./scripts/run_unit_tests.sh -q` (or `--parallel`).
- E2E: optional Telethon test `TELEGRAM_E2E=1 pytest -m telegram_e2e tests/e2e_telegram` (needs secrets; not a gate).
- Local ports (bind `127.0.0.1`): Mongo 27017, Redis 6379, bot 5000, Weaviate 8080, Hermes 8090, Santiment 8091,
  market oracle 8092, cortex 8765. Note: `aria_bot.py` defaults to `HOST=0.0.0.0`; set `HOST=127.0.0.1` locally.
- Deploy: Railway. Service `xagent-test` (env `test`) auto-deploys from `staging` (paper bot; sidecars such as
  `xagent-mcp`, `xagent-hermes`, `xagent-memory-cortex` via `railway.cortex.toml`). Production from `main` (described
  as "future/later" in `docs/RAILWAY_PLAN.md`). Build: `Dockerfile` + `railway.toml` (`scripts/railway_start.sh`, health `/health`).
- Who may deploy: Jens only (apart from the `staging` auto-deploy that a permitted merge to `staging` triggers). Agents never run `scripts/deploy_staging.sh`, `railway variables`, or any Railway write; reading deployment status and logs to verify the `staging` auto-deploy is allowed.
- Whether Railway is the only deploy target, and where the real-money bot runs today: OPEN (see section 10).

## 5. Critical risks

- Real money: wrong order sizing, stops, DCA, exits or risk limits lose money on the live Gate account.
- Live state corruption: `*.live.json` ledgers, Mongo `xagent` / `xagent_test` ledger (never rename, reset or wipe).
- Unintended deploy: any push or merge to `staging` redeploys the paper bot; `main` is the live/release branch.
- Secret leakage: exchange keys, Telegram/X/xAI tokens, Railway/Mongo URLs.
- High-risk paths: `aria_bot.py`, `execution/`, `risk/`, `*.live.json`, plus `ci/money_paths.json`:
  `strategies/`, `services/gainer_signal/`, `services/dca_sniper/`, and `config.json` keys starting with `risk`,
  `costs`, `max_usdt_per_trade`, `max_position_percent`, `stop_loss_pct`, `max_open_positions`, `max_daily_trades`,
  `trade_cooldown_hours`. Also deploy config (`Dockerfile`, `railway*.toml`, `scripts/railway_start.sh`,
  `deploy/live_caps_overlay.json`) — proposed as high-risk, see section 10.
- Further code that places or sizes orders or rewrites ledgers outside `ci/money_paths.json` (found 2026-11-04,
  proposed as high-risk, see section 10): `services/exit_realtime/` (realtime exits, `execute.py`), `services/exit_radar*`,
  `services/trading_service.py`, `services/portfolio_service.py`, `services/order_service.py`, `services/mcp/bot_http.py`
  (MCP buy/sell/lock), `notifications/telegram_commands/manual_order_flow.py`,
  `core/costs.py`, `core/execution_mode.py`, `core/config_guardrails.py`, `data_manager.py`,
  `scripts/ledger_epoch.py` and `bus/writer_lease.py` (ledger reset/restore), and every `config.json` change that
  also applies to `main`/live.

## 6. Tests

- Levels: unit (`tests/unit`, default target of `run_unit_tests.sh`), property tests (hypothesis), integration
  (`-m integration`, optional, slow), Telegram E2E (`-m telegram_e2e`, optional), backtests (`python -m backtest.harness`).
- Must be green before a PR: local unit suite; in CI the required check `unit`
  (`classify` + `pytest` + `review-gates` + `hypothesis-evidence` + `ruff`). PR body needs an `Evidence` heading or a
  `docs/evidence/*.md`; money-path PRs need `owner-approved` (Jens, or Kay for staging merges) and `docs/evidence/<pr-or-issue>.md`.
- Coverage expectations: no coverage threshold in CI (OPEN). Rule: existing assertions frozen, new behavior gets new tests.
- Mobile viewports: Telegram UI, no web viewports defined; desk UI viewports OPEN (proposed 1280x800 and 390x844).

## 7. No-go areas

- Deploy anywhere (Railway or other), run deploy scripts, change Railway variables, write to Railway Mongo.
- Change live config or `*.live.json`, enable live trading, place orders, call bot MCP buy/sell/lock.
- Create accounts or paid services.
- Read or print `.env`, `.env.local`, `ngrok.log`, credentials files, tokens, `.cursor/mcp.json`.
- Merge (exception: Kay may merge proven paper-only improvements into `staging` under `AGENTS.md` rule 6; never PRs on `aria_bot.py`, `execution/`, `risk/`, `*.live.json` or real money), push to `main` or `staging`, set `owner-approved` (exception: Kay for staging merges) or Omnigent labels, use `ALLOW_DEPLOY_PUSH=1`.
- Edit an existing test assertion to make a change pass.
- Hardcode coin names in strategy or risk code.

## 8. Definition of Done

- [ ] Ticket and PR title in English; spec with ACs for behavior changes.
- [ ] Unit suite green locally (unique `PYTEST_DB_SUFFIX`), `data/` untouched.
- [ ] CI `unit` green.
- [ ] PR body has an `Evidence` section with before/after metric; money path: `docs/evidence/<pr-or-issue>.md`.
- [ ] DeepSeek review passed and posted as a PR comment naming the model; Claude Opus final review passed for high-risk paths / before going live (interim until 2026-11-06 07:00 Europe/Berlin: DeepSeek final review, Opus catch-up afterwards); tester sign-off.
- [ ] Docs updated (`AGENTS.md`, `docs/`) when rules or behavior change.
- [ ] No secrets in diff, logs or PR text.
- [ ] Merged by Jens, or by Kay into `staging` under the merge rule below; `staging` auto-deploy verified read-only (SUCCESS) and logged.

## 9. Workflow and roles

See [`docs/workflow.md`](workflow.md). Release naming and the blocked version gate: [`docs/release-framework.md`](release-framework.md). A raw request is not a build-ready ticket: [`docs/requirements-engineering-guide.md`](requirements-engineering-guide.md) §1a item 7. Lead: Kay (Grok Bot lead). Coder: Grok 4.7 (`grok-4.7`). Reviewer: DeepSeek
(`deepseek-v4-pro`) per PR. Final reviewer: Claude Opus (`claude-opus-5-5`), mandatory. Tester: separate role.
Branch naming `<type>/<slug>-<issue>` (`fix/`, `feat/`, `docs/`, `chore/`, `perf/`, `test/`, `ci/`, `hotfix/`,
`cursor/`), one worktree per task under `/Users/jholze/Documents/scripts/trading_bot_omnigent/.worktrees/<slug>`, PR
against `staging`, merge to `main` only by Jens; Kay may merge into `staging` as below. The older directory `trading_bot-worktrees/` is historical inventory (open question 15), not the active workspace. PR template: none in `.github/` (OPEN). Issue template: none under `.github/ISSUE_TEMPLATE` (OPEN). Back channel: a session in the Omnigent
project `adba8ee6896849cd82e22bec7a881691`, addressed to Jens' assistant. Sessions and tickets for Kay go through `kay-entry` (or Pflichtblock + Hindsight) before any raw MCP create.

Merge rule for this project (Jens, 2026-11-04; replaces the 2026-11-01 22:57 rule): Kay may merge into `staging` only proven improvements with evidence in the PR (tests, backtest or paper trading), for paper trading only, with a passed review gate and green CI `unit`. Jens keeps `main`, every deploy, the live paths `aria_bot.py`, `execution/`, `risk/`, `*.live.json`, and anything with real money: for those Kay opens the PR and stops. Code reaches `main` only through the `staging` → `main` release PR, opened and merged by Jens.

Model rule: Grok 4.7 codes, DeepSeek reviews every PR, Claude Opus final review (mandatory, real money); until 2026-11-06 07:00 Europe/Berlin DeepSeek gives the final review and Opus re-reviews the high-risk PRs afterwards. Every PR names the reviewing model. At least two model families per PR, author and reviewer never the same family.

## 10. Open questions

1. **Key success metric.** Proposed default: net PnL after fees/slippage vs. buy-and-hold at bounded max drawdown, on paper first.
2. **Lead agent in Omnigent.** The project config has `default_agent: xagent_lead` with `agent_id` `58a1bc5bf0bba6d31ceeb7661f8d751c`, but that id is the built-in `claude-native-ui` agent, `list_projects` shows `default_agent: null`, and `xagent_lead` (agent id `74cf5567…`, last sessions 2026-09-19) is no longer registered. Proposed default: Kay (Grok Bot lead) leads via the `trading-bot` account; Jens either registers a lead agent or clears `default_agent`.
3. **Where the live (real-money) bot runs.** Docs call Railway production "future"; the Mac has production start scripts (`scripts/start_stack.sh`). Proposed default: treat both the Mac main checkout and any Railway production service as live and off-limits.
4. **Tenants.** Who they are and whether tenant trading is real money. Proposed default: treat every tenant as real money.
5. **Coder model config.** `.grok/settings.json` sets `grok-code-fast-1`, but the model rule says Grok 4.7. Proposed default: Jens updates it to `grok-4.7` (config change, not done by agents).
6. **Python typecheck.** None configured. Proposed default: no new gate; Ruff + tests only.
7. **Coverage threshold.** None in CI. Proposed default: no global threshold; every AC has a test.
8. **Desk UI viewports.** Proposed default: check 1280x800 and 390x844 for desk UI changes.
9. **PR template and issue template.** None in `.github/` (no pull-request template, no `.github/ISSUE_TEMPLATE`). Proposed default: add a PR template with Summary / Spec (ACs) / Evidence / Risk (money path yes/no) / Reviews, and an issue template with **Existing in code** plus **Existing in memory (Hindsight)** (`memory:<id>` / `doc:<id>`, or `RECALL_FAILED`). Until that template exists, the issue body still carries those sections (`AGENTS.md` rule 0, `docs/workflow.md`).
10. **Deploy config as high-risk.** `Dockerfile`, `railway*.toml`, `scripts/railway_start.sh`, `deploy/live_caps_overlay.json` are not in `ci/money_paths.json`. Proposed default: treat as high-risk (Claude Opus final review), Jens decides whether to add them to the gate.
11. **Local bind default.** `aria_bot.py` defaults to `HOST=0.0.0.0`. Proposed default: agents always set `HOST=127.0.0.1`; a code change to the default needs a ticket (high-risk file).
12. **Telegram E2E / staging checks as gates.** Only `unit` is required. Proposed default: E2E stays optional; staging soak (`docs/WQE_STAGING_SOAK.md`) is Jens' post-merge check.
13. **Further high-risk paths.** Code that places/sizes orders or rewrites ledgers outside `ci/money_paths.json` (list in section 5). Proposed default: treat as high-risk (Opus final review, Jens merges when real money is involved); Jens decides whether to add them to `ci/money_paths.json`.
14. **`config.json` applies to `main`/live too.** A paper-motivated `config.json` change also changes live behaviour after the next release. Proposed default: every `config.json` change is flagged in the release PR for Jens.
15. **Worktree cleanup on the Mac.** About 91 task worktrees under `trading_bot-worktrees/` (inventory 2026-11-01: keep 6, archive 21, delete-candidate 65), plus `chore-hermes-enabled-false-310`, an empty folder skeleton without `.git`. A folder `trading-bot-worktrees/` (with hyphen) does not exist (checked 2026-11-01 and 2026-11-04). Proposed default: Jens removes the delete-candidates himself; agents delete nothing.
