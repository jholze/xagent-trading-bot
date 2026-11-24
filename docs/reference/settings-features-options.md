# Settings, features, options (agent reference)

Diátaxis **reference** for coding/ops agents. Humans still use [README.md](../../README.md) and [DOCUMENTATION.md](../../DOCUMENTATION.md).

## How to use

1. Read the generated tables: [settings-features-options.generated.md](settings-features-options.generated.md).
2. Confirm the value in committed `config.json` (this branch / `staging` = paper). Tenant overlays and Railway env can differ — never invent env secrets; never run `railway variables`.
3. Find behaviour in code (`core/config.py`, `notifications/telegram_commands/`, strategy modules).
4. Before research, analysis, or filing a ticket: also follow the **Docs + Hindsight** rule in [AGENTS.md](../../AGENTS.md).

## Shorts sandbox and testnet route keys (#826)

These keys are not in committed `config.json`. The generated tables only list committed keys, so they are described here.

| key | default | meaning |
| --- | --- | --- |
| `shorts.sandbox_expiry_v220.execution_budget_seconds` | absent | Non-negative int. Seconds reserved for executing a cover of a sandbox-tagged short (`sandbox_path = v220_paper`). Used only if `reconcile_budget_seconds` is also set. With both set, runtime cover is due at `min(entry + 4h, 20:00 Europe/Berlin) - (execution + reconcile)`. |
| `shorts.sandbox_expiry_v220.reconcile_budget_seconds` | absent | Non-negative int. Seconds reserved for reconciling that cover. Used only together with the key above. If either key is absent, the lot's own stamped budgets apply. If the lot has none, it is `buffer_unknown` and is covered as overdue. |
| `shorts.testnet_futures.strict_route` | absent (off) | Literal `true` only. Runs the #804 route guard before testnet SHORT/COVER. With it on, production denies every testnet SHORT/COVER until a ModeProof producer and a separate testnet key env exist. Off leaves the existing adapter path unchanged. |

## Regenerating

```bash
python3 scripts/generate_settings_reference.py
```

CI workflow `docs-settings-freshness` fails if the generated file is stale.

## README defaults caveat

The README “Wichtige Defaults” table historically drifted from `config.json`. Prefer this reference and the committed JSON. Do not copy README numbers into tickets without re-reading `config.json` on the target branch.

## Hindsight note (2026-11-05)

Bank `coding-agent::trading_bot` held stale process memory (pre–2026-11-04 lead/merge story). Prefer `AGENTS.md` + this reference for process and settings. Query Hindsight for lessons/decisions, then verify against git/`config.json`. Omnigent MCP for bot account `trading-bot` does **not** expose Hindsight tools; use the Hindsight coding-agent tools or local HTTP API on the Mac.
