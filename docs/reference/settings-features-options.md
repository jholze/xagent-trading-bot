# Settings, features, options (agent reference)

Diátaxis **reference** for coding/ops agents. Humans still use [README.md](../../README.md) and [DOCUMENTATION.md](../../DOCUMENTATION.md).

## How to use

1. Read the generated tables: [settings-features-options.generated.md](settings-features-options.generated.md).
2. Confirm the value in committed `config.json` (this branch / `staging` = paper). Tenant overlays and Railway env can differ — never invent env secrets; never run `railway variables`.
3. Find behaviour in code (`core/config.py`, `notifications/telegram_commands/`, strategy modules).
4. Before research, analysis, or filing a ticket: also follow the **Docs + Hindsight** rule in [AGENTS.md](../../AGENTS.md).

## Regenerating

```bash
python3 scripts/generate_settings_reference.py
```

CI workflow `docs-settings-freshness` fails if the generated file is stale.

## README defaults caveat

The README “Wichtige Defaults” table historically drifted from `config.json`. Prefer this reference and the committed JSON. Do not copy README numbers into tickets without re-reading `config.json` on the target branch.

## Hindsight note (2026-11-05)

Bank `coding-agent::trading_bot` held stale process memory (pre–2026-11-04 lead/merge story). Prefer `AGENTS.md` + this reference for process and settings. Query Hindsight for lessons/decisions, then verify against git/`config.json`. Omnigent MCP for bot account `trading-bot` does **not** expose Hindsight tools; use the Hindsight coding-agent tools or local HTTP API on the Mac.
