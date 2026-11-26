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

Continuous score in [-1, 1] from the market-oracle feature poll. Numbers live only in committed `config.json` under `rule_switches.oracle_score`. They are the design table in issue comment 7185819947. The code reads that block. It does not recompute `MarketDataClient.fetch_features` and it does not start a Santiment fetch.

Mode comes from the in-memory tenant-effective config. No disk read and no Mongo read on the buy path. Order: `tenant_mode_override` when that key is set, else `tenant_modes[<tenant_id>]`, else `default_mode`. The committed key is `default_mode`, not `mode`. A save-back of the merged config cannot pin an override, and a copied `mode` key is ignored. Only the literals `off`, `shadow` and `enforce` are modes. Anything else, including `true`, `on`, `yes` and `1`, is `off`. A blank override falls through. Committed `default_mode` is `off`. Committed `tenant_modes.henry` is `shadow`. Henry's existing `trend_filter` values are not in this block and are not changed (`sma_days` 3, `max_extension_pct` 20, `max_pump_24h_pct` 20).

| key | committed | meaning |
| --- | --- | --- |
| `rule_switches.oracle_score.default_mode` | `off` | Used when the tenant has no `tenant_mode_override` and no `tenant_modes` entry. `off`: trend filter unchanged and no extra score I/O. `shadow`: same decision, plus one `oracle_score_shadow` log line and a bounded `obs_oracle_score_shadow` enqueue. `enforce`: trend filter uses the score's `sma_days` and `max_extension_pct` only. At `s >= sma_off_at` the SMA condition is skipped via a keyword argument (never a `trend_filter` config key) and extension is measured on `sma_floor` bars. |
| `rule_switches.oracle_score.tenant_mode_override` | absent | The only tenant override key. Set it on a tenant body to force one mode. |
| `rule_switches.oracle_score.tenant_modes` | `{"henry": "shadow"}` | Used when `tenant_mode_override` is absent or blank. |
| `rule_switches.oracle_score.half_life_sec` | `21600` | EMA half-life. Alpha is `1 - 0.5^(dt/half_life)` from the real gap between polls. |
| `rule_switches.oracle_score.max_gap_sec` | `21600` | A longer gap resets the EMA to the raw score. |
| `rule_switches.oracle_score.stale_after_sec` | `2400` | Older than this, enforce uses the fixed tenant filter (never the score). This is the default heartbeat (1800s) plus two default polls (300s each), so it is longer than the real push interval. The oracle pushes whenever `oracle_score.ts` advances, which is every successful poll. |
| `rule_switches.oracle_score.history_retain_sec` | `2592000` | Score history (`obs_oracle_score`) older than this is dropped. Mongo history is best-effort on a background thread. `serverSelection`, socket and connect timeouts are each `mongo_timeout_ms`. |
| `rule_switches.oracle_score.history_min_interval_sec` | `300` | Minimum spacing between history rows. |
| `rule_switches.oracle_score.mongo_timeout_ms` | `2000` | Per-call Mongo timeout for the history writer. The poll does not wait on it. |
| `rule_switches.oracle_score.redis_timeout_sec` | `0.5` | Socket and connect timeout for the EMA restart key `market_oracle:score_state`. |
| `rule_switches.oracle_score.persist_error_interval_sec` | `300` | The first failed persist logs ERROR. Later failures log at most once per this interval. |
| `rule_switches.oracle_score.components` | 7 entries | `m24` is a blend: `(0.7·btc_ret_24h_pct + 0.3·eth_ret_24h_pct) / 4`, weight 0.25. `m4` is `btc_ret_4h_pct / 2`, weight 0.15. `m1` is `btc_ret_1h_pct / 1.5`, weight 0.05. `trend` is `btc_trend_4h`, weight 0.20. `breadth` is `(breadth_pct_green - 0.5) / 0.25`, weight 0.25. `funding` is `-(btc_funding_rate_pct - 0.01) / 0.04`, weight 0.05. `sentiment` is `santiment_composite`, weight 0.05. Each result is clipped to [-1, 1]. A missing blend leg drops that component. Missing components are dropped and the remaining weights are renormalised. |
| `rule_switches.oracle_score.components` sentiment | configured, not filled | `market_policy_fusion` sentiment is a regime-bucket lookup, and the Santiment composite is not on the oracle feature poll. The oracle process does not import fusion and does not start a Santiment call, so this component stays missing. |
| `rule_switches.oracle_score.mapping` | formula coefficients | Relative to the tenant's own base, so `s = 0` reproduces that base. `sma_tight_days` 50, `sma_neg_span` 0.8, `sma_pos_span` 0.6, `sma_floor` 2, `sma_off_at` 0.8, `extension_slope` 0.5, `floor_base` 2, `floor_exponent` 1, `size_round_up_frac` 0.5. Lengths use half-up rounding. |

`s < 0`: `sma_days = round_half_up(base · (50 / base) ^ min(-s / 0.8, 1))`. `0 <= s < 0.8`: `sma_days = max(2, round_half_up(base - (base - 1) · min(s / 0.6, 1)))`. `s >= 0.8`: the SMA condition is off. Half-up is `floor(x + 0.5)`, not Python's banker's `round` (`round(2.5)` is 2; half-up is 3). `max_extension_pct = base · (1 + 0.5 · s)`. Long mcap floor and liquidity floor are `base · 2^(-s)`. Size below `min_trade` may be rounded up when the shortfall is within `0.5 · max(s, 0)` of the minimum; that fraction is logged only. `max_pump_24h_pct` is not mapped. For `s <= 0`, a mapped SMA shorter than the tenant base, or an extension wider than the tenant base, is clamped back to the base.

The fixed decision fetches `sma_days + 10` bars. The score variant fetches separately. A failed or short extra fetch only drops the score check. It does not change the decision, including in enforce. A null raw score publishes nothing new. The EMA restart state is Redis (`market_oracle:score_state`). Mongo history does not block the poll. The oracle service's Mongo URI was not read (`railway variables` is not run); history stays best-effort.

A missing or stale score, and any score exception, leave the buy on the fixed tenant filter. The oracle poll still publishes its snapshot when the score step fails.

## Regime-gated climax shorts (`shorts.climax_fade.regime_gate`, `shorts.trend_short_shadow`, #836)

The gate applies to climax-fade **opens** only. Cover, stop, and time doors are unchanged. Thresholds, `exclude_symbols`, and `size_factor` are unchanged. `shorts.enabled`, `shorts.auto_after_sell`, and `shorts.allow_live` are unchanged.

The smoothed score comes from the #835 reader (`read_published_score`). Freshness is `score_is_fresh` on that block's `ts` (advanced on every successful poll) against the in-memory `rule_switches.oracle_score.stale_after_sec` (committed `2400`). The gate does not read the config file, does not recompute the score, and does not use the Redis EMA state.

| key | committed | meaning |
| --- | --- | --- |
| `shorts.climax_fade.regime_gate.mode` | `enforce` | `off`: the score is not read and opens behave as before. `shadow`: a failing check logs `climax_fade_regime_gate would_block` and the open still runs. `enforce`: the open runs only when the smoothed score exists, is fresh, and is `<= max_score`. Otherwise the open returns before the 4h fetch and logs `climax_fade_regime_gate blocked symbol=… s=… age_s=… max=… reason=…` once per symbol per closed 4h bar. The mode is `off` only when the key is absent or the block is missing. A present value that is not `off`, `shadow`, or `enforce`, including null, `""`, and false, is `enforce`. |
| `shorts.climax_fade.regime_gate.max_score` | `-0.2` | Upper bound on the smoothed score for an open. `-0.2` is the lower edge of the neutral bucket. A missing or non-finite value does not open in `enforce`. |
| `shorts.trend_short_shadow.enabled` | `true` | Observe only. Literal `true` only. Evaluated once per closed bar on symbols `process_coin` already scans. Never calls `execute_order` and never takes a slot. |
| `shorts.trend_short_shadow.fast_hours` | `48` | Fast EMA length in hours. The span in bars is `fast_hours / timeframe hours` (48/4 = 12). |
| `shorts.trend_short_shadow.slow_hours` | `480` | Slow EMA length in hours. The span in bars is `slow_hours / timeframe hours` (480/4 = 120). |
| `shorts.trend_short_shadow.timeframe` | `4h` | Only `4h` is accepted. Any other value disables the shadow and logs one `trend_short_shadow disabled … reason=only_4h` WARNING. |
| `shorts.trend_short_shadow.warmup_mult` | `3` | Bars requested = `warmup_mult * slow span` (3 × 120 = 360) so the slow EMA can converge. |
| `shorts.trend_short_shadow.round_trip_cost_pct` | `0.7` | Round-trip cost in percent stored on each `obs_trend_short_shadow` row (0.70%). A later job uses it for the 72h outcome. |

A cross of the fast EMA below the slow EMA logs `trend_short_shadow would_open symbol=… close=… ema_fast=… ema_slow=… s=… n_bars=…` and writes `obs_trend_short_shadow` (the row includes `n_bars`) only when the closed-bar count is at least the configured warmup. A cross above logs `would_cover`. Climax `exclude_symbols` are still logged, with `excluded=true`. The EMA state is recomputed from the fetched window. A None or empty frame logs `trend_short_shadow no_data` once per symbol per closed bar and is not fetched again that bar. A frame whose latest closed bar is the expected bar but shorter than the warmup is not fetched again that bar, and the cross does not count. A raising fetch logs `trend_short_shadow error` once per symbol per closed bar and does not raise.

## Regenerating

```bash
python3 scripts/generate_settings_reference.py
```

CI workflow `docs-settings-freshness` fails if the generated file is stale.

## README defaults caveat

The README “Wichtige Defaults” table historically drifted from `config.json`. Prefer this reference and the committed JSON. Do not copy README numbers into tickets without re-reading `config.json` on the target branch.

## Hindsight note (2026-11-05)

Bank `coding-agent::trading_bot` held stale process memory (pre–2026-11-04 lead/merge story). Prefer `AGENTS.md` + this reference for process and settings. Query Hindsight for lessons/decisions, then verify against git/`config.json`. Omnigent MCP for bot account `trading-bot` does **not** expose Hindsight tools; use the Hindsight coding-agent tools or local HTTP API on the Mac.
