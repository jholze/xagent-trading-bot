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
| `shorts.testnet_futures.strict_route` | absent (off) | Literal `true` only. Runs the #804 route guard before testnet SHORT/COVER and (#831) before every `execution/recovery.py` startup read. With it on, a SHORT/COVER needs the #830 ModeProof (the adapter's own isolated `set_leverage` result plus a fresh `fetch_position` readback, max age `shorts.testnet_futures.paper_start.mode_proof_max_age_seconds`, ceiling 60 s) and the dedicated testnet key names below; a recovery read needs a testnet sandbox client, the pinned host on every private API base and a no-redirect session. Any denial: no order or read is sent; recovery aborts for that tenant/scope (`RecoveryFailed`, cycle skipped). Off leaves the existing adapter and recovery paths unchanged. |
| `shorts.testnet_futures.api_key_env` | absent → `GATE_TESTNET_API_KEY` | #830. Env var *name* (never the value) of the dedicated Gate testnet API key. Used only in testnet mode with `strict_route: true`; then the testnet client signs with it instead of `live.api_key_env`. Denied (`testnet_credentials_unknown`) if the name is empty or the env var is unset/empty; denied (`testnet_env_aliases_live`) if the name equals `live.api_key_env` / `live.api_secret_env` or the value equals a live value. Values are compared in process, never logged or returned. |
| `shorts.testnet_futures.api_secret_env` | absent → `GATE_TESTNET_API_SECRET` | #830. Env var *name* of the dedicated testnet API secret. Same rules as `api_key_env`; key and secret names must differ. |

## Oracle regime score (`rule_switches.oracle_score`, #835)

Continuous score in [-1, 1] from the market-oracle feature poll. Numbers live only in committed `config.json` under `rule_switches.oracle_score` (weights, scales, half-life, max gap, staleness, history retention, and the mapping anchors). The code reads that block. It does not recompute `MarketDataClient.fetch_features`.

Mode, per tenant, in this order: the tenant body's `rule_switches.oracle_score.mode` when that key is set, else committed `tenant_modes[<tenant_id>]`, else committed `mode`. Committed `mode` is `off`. Committed `tenant_modes.henry` is `shadow`. Henry's existing `trend_filter` values are not in this block and are not changed (`sma_days` 3, `max_extension_pct` 20, `max_pump_24h_pct` 20).

| key | committed | meaning |
| --- | --- | --- |
| `rule_switches.oracle_score.mode` | `off` | `off`: trend filter unchanged, score is not read. `shadow`: same decision, plus one `oracle_score_shadow` log line and an `obs_oracle_score_shadow` row. `enforce`: trend filter uses the score's `sma_days` and `max_extension_pct` only. Unknown values are `off`. |
| `rule_switches.oracle_score.tenant_modes` | `{"henry": "shadow"}` | Used when the tenant body does not set `mode`. |
| `rule_switches.oracle_score.half_life_sec` | `21600` | EMA half-life. Alpha is `1 - 0.5^(dt/half_life)` from the real gap between polls. |
| `rule_switches.oracle_score.max_gap_sec` | `21600` | A longer gap resets the EMA to the raw score. |
| `rule_switches.oracle_score.stale_after_sec` | `1800` | Older than this, enforce uses the fixed tenant filter (never the score). |
| `rule_switches.oracle_score.history_retain_sec` | `2592000` | Score history (`obs_oracle_score`) older than this is dropped. |
| `rule_switches.oracle_score.history_min_interval_sec` | `300` | Minimum spacing between history rows. |
| `rule_switches.oracle_score.sma_days_min` | `1` | Floor under the mapped `sma_days` after the integer step. The s=+0.8 anchor is 2, because a 1-bar SMA equals the close and `close > SMA` cannot pass. |
| `rule_switches.oracle_score.components` | 7 entries | `name`, `feature`, `weight`, `scale`, `center`, `sign`. Missing features are dropped and the remaining weights are renormalised. |
| `rule_switches.oracle_score.mapping.reference` | sma 3 / ext 20 / pump 20 / mcap 5_000_000 / liq 50_000 | Denominator. Mapped values scale by the tenant base divided by this reference, so s=0 returns the tenant's own base. |
| `rule_switches.oracle_score.mapping.anchors` | s = -1, 0, +0.6, +0.8 | Linear interpolation. Outside the span the nearest anchor is held. Enforce applies only `sma_days` and `max_extension_pct`. Pump, mcap floor, liquidity floor and `size_round_up_usdt` are logged only. |

A missing or stale score, and any score exception, leave the buy on the fixed tenant filter. The oracle poll still publishes its snapshot when the score step fails.

## Regenerating

```bash
python3 scripts/generate_settings_reference.py
```

CI workflow `docs-settings-freshness` fails if the generated file is stale.

## README defaults caveat

The README “Wichtige Defaults” table historically drifted from `config.json`. Prefer this reference and the committed JSON. Do not copy README numbers into tickets without re-reading `config.json` on the target branch.

## Hindsight note (2026-11-05)

Bank `coding-agent::trading_bot` held stale process memory (pre–2026-11-04 lead/merge story). Prefer `AGENTS.md` + this reference for process and settings. Query Hindsight for lessons/decisions, then verify against git/`config.json`. Omnigent MCP for bot account `trading-bot` does **not** expose Hindsight tools; use the Hindsight coding-agent tools or local HTTP API on the Mac.
