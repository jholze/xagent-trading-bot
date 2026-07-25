# Local stack — Railway staging parity (Mac)

> **GitHub:** [#132](https://github.com/jholze/xagent-trading-bot/issues/132)  
> **Goal:** Same *service shape* as Railway env `test`, on your Mac, before `deploy_staging.sh`.

## Service map

| Railway | Local command | Default port |
|---------|---------------|--------------|
| Mongo plugin | brew Mongo **or** compose `--full` | `27017` |
| Redis plugin | `ensure_redis` / brew **or** compose `--full` | `6379` |
| `xagent-test` | `bash scripts/local_stack_bot.sh` | `5000` + ngrok |
| `xagent-hermes` | `bash scripts/local_stack_hermes.sh` | `8090` |
| `xagent-weaviate` | compose service `weaviate` | `8080` |
| `xagent-santiment` | `bash scripts/local_stack_sidecars.sh` | `8091` |
| `xagent-market-oracle` | (same sidecars script) | `8092` |
| `xagent-memory-cortex` | `bash scripts/local_stack_cortex.sh` | `8765` |

**Hard rule:** never export Railway `MONGO_URL` into the Mac shell for everyday runs.  
Scripts call `dev_local_mongo.sh` and **unset** `MONGO_URL`.

## Quick start (Tier A — most features)

```bash
# 0) secrets (once)
# .env + .env.local with DEV Telegram bot token

# 1) infra
bash scripts/local_stack_up.sh

# 2) health
bash scripts/local_stack_health.sh --infra

# 3) bot (= xagent-test)
bash scripts/local_stack_bot.sh
```

Telegram should hit the bot via ngrok like today (`start_demo_with_ngrok.sh`).

## Tier B — Memory stack

```bash
bash scripts/local_stack_up.sh          # includes Weaviate
bash scripts/local_stack_hermes.sh --bg # PORT 8090
# optional UI
bash scripts/local_stack_cortex.sh --bg
bash scripts/local_stack_health.sh
```

Hermes is **read-only** on ledger (same as Railway `xagent-hermes`).

## Tier C — Full sidecars

```bash
bash scripts/local_stack_sidecars.sh    # santiment + market-oracle
# stop:
bash scripts/local_stack_sidecars.sh --stop
```

Defaults: `DRY_RUN=1` so sidecars do not push aggressively without config.

## Full Docker infra

If you do not want brew Mongo/Redis:

```bash
bash scripts/local_stack_up.sh --full
```

Starts compose profile `full`: mongo + redis + weaviate.

## Config

```bash
cp deploy/local/env.stack.example deploy/local/env.stack
# edit ports / HERMES_RUN_LEARNING / etc.
```

`deploy/local/env.stack` is gitignored.

## Stop

```bash
bash scripts/local_stack_down.sh          # sidecars + hermes pids + docker
bash scripts/local_stack_down.sh --bot    # also stop_bot.sh
bash scripts/local_stack_down.sh --volumes  # wipe weaviate/mongo docker volumes
```

Host brew Mongo/Redis stay up unless you stop them yourself.

## Counter-test ritual (before staging)

One gate for “can we ship this feature to staging?”:

```bash
# Infra + pre-staging smokes + recommended unit slice
bash scripts/local_stack_test.sh --up --unit
```

| Flag | What it runs |
|------|----------------|
| (none) | Infra health + `verify_pre_staging.sh` |
| `--up` | `local_stack_up.sh --full` first |
| `--unit` | High-signal unit slice (orders, ledger, memory, dry-run) |
| `--full-unit` | All `tests/unit` (slow) |

Python: scripts auto-pick **3.13 with pymongo** (not bare Homebrew 3.14).  
Override: `export LOCAL_STACK_PYTHON=/path/to/python3.13`

Manual steps after the gate is green:

```bash
bash scripts/local_stack_bot.sh     # operator path / Telegram
# exercise the feature
bash scripts/local_stack_health.sh
# then from branch staging: bash scripts/deploy_staging.sh
```

## Port rationale

On Railway each service uses `PORT=8080` internally. Locally they share one host, so:

| Process | Port |
|---------|------|
| Bot | 5000 |
| Weaviate | 8080 |
| Hermes | 8090 |
| Santiment | 8091 |
| Market oracle | 8092 |
| Cortex | 8765 |

## Related

- Issue [#132](https://github.com/jholze/xagent-trading-bot/issues/132)
- `scripts/railway_start.sh` — same Python modules
- `docs/RAILWAY_PLAN.md`
- Headless Telegram / full DoD: `plans/telegram-headless-testing.md` (when present)
