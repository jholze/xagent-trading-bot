# Agent operating rules

This repository is a **paper** trading bot. Branch `staging` is the running paper deployment: merging to `staging` deploys it. Open pull requests against `staging` only. Never push or merge to `main`. Never enable live trading. Never add API keys, exchange secrets, or credentials.

The owner is Jens Holze. Agents do not merge.

## Spec before code

Every behavior change starts with a written spec: numbered acceptance criteria `AC1`…`ACn` and the acceptance tests that pin them. Freeze the spec before building. A later change is `Rev N` and does not rewrite the previous revision in place. Standard: `docs/domain/spec-writing.md`.

## Tests are the spec

Existing test assertions are frozen. Add tests freely. Change an existing assertion only when the spec names the behavior that changes and why. Report a red test (which test, what it asserted, what it got). Do not edit it to match new behavior. A test diff is a contract change.

## Evidence

Every change must be measurably better. Put a before/after metric on the pull request: tests run, mutants killed, backtest values after fees, risk metrics, or paper results. The required check is still the job named `unit`. Pytest may be skipped for an allowlisted documentation diff. The evidence gate, hypothesis JSON check, and changed-file Ruff still run, and `unit` fails if they fail (`scripts/ci/review_gates.py`, rules in `ci/money_paths.json`).

## Money path

Entries, exits, stops, DCA, sizing, risk limits, config limits, and locks are the money path. The path list is `ci/money_paths.json` and is the list the gate uses. A money-path pull request fails `unit` unless it has the label `owner-approved` and a file `docs/evidence/<pr-or-issue>.md` with a before/after table. The owner decides. Do not add `owner-approved` yourself.

## Symbols

No hardcoded coin names in strategy or risk code. Symbol and source rules live in config. Tests use fixture configs.

## Test isolation

- Run units with `./scripts/run_unit_tests.sh`. `--parallel` pins local Mongo and isolates ledgers per test.
- Two suites at once need distinct `PYTEST_DB_SUFFIX` values. xdist workers append `_gwN`. The same suffix from two worktrees shares `xagent_pytest` and makes results order-dependent.
- Tests must not write `data/`. Ledger files are redirected per test. A new test that touches another data file patches that path to `tmp_path`.
- Unit tests cannot reach the network. Mock the fetcher (`tests/support/offline.py`).

## Domain

Review knowledge that used to live with people is in `docs/domain/`. Use the numbers written there. Values marked open or unknown are not decided; do not fill them in.

## What this file replaces

`CLAUDE.md` is a pointer plus a list of retired instructions (Grok delegation, self-merge to `staging`, Omnigent labels, `rebuild/*` as the integration branch). Do not follow those. This file is the operating source of truth.
