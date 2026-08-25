# DCA Sniper — **standalone service** (`xagent-dca-sniper`)

Own Railway container. Talks to **bot** (HTTP internal APIs) + **Redis** (state/wake/prices) + **Gate WS** (focus ticks).

```
┌─ xagent-dca-sniper ─────────────────────────────┐
│  poll loop + Gate WS + Redis pub/sub wake         │
│  rank / reclaim / size                             │
└───────────┬───────────────────┬─────────────────┘
            │ HTTP token        │ Redis
            ▼                   ▼
     xagent-test           Redis (shared)
     /internal/dca-sniper/*  aria:dca_sniper:*
     (execute, ledger)       health, state, wake
```

## Railway

Service name: **`xagent-dca-sniper`** (same image as bot; `scripts/railway_start.sh` routes by name).

| Variable | Purpose |
|----------|---------|
| `RAILWAY_SERVICE_NAME=xagent-dca-sniper` | start selector |
| `DCA_SNIPER_ENABLED=1` | run cycles |
| `DCA_SNIPER_TOKEN` | same as bot internal token (`EXIT_WS_INTERNAL_TOKEN` ok) |
| `DCA_SNIPER_BOT_URL` | `https://xagent-test-test.up.railway.app` (or private URL) |
| `REDIS_URL` | shared Redis (auto on Railway if linked) |
| `MONGO_URL` | **required for deep path** — same Mongo as bot (`${{Mongo.MONGO_URL}}`) |
| `MONGODB_DB` | default `xagent_test` |
| `DEMO_ALLOW_REMOTE_MONGO=1` | allow Railway Mongo (not localhost) |
| `DEMO_LEDGER_BACKEND=mongo` | cash_policy / facts read path |
| `DCA_SNIPER_POLL_SEC` | default 180 |
| `PORT` | health (Railway) |

Sniper does **not** write orders; execute still goes through bot HTTP. Mongo is for
read-only deep context (cash mode, memory lessons, facts). Without `MONGO_URL` the
process falls back to `127.0.0.1:27017` and spams `Mongo orders load failed`.

**Bot** (`xagent-test`):

| Variable / config | Purpose |
|-------------------|---------|
| `dca_sniper.enabled=true` | cycle DCA off; authority |
| `dca_sniper.in_process_tick=false` | **must be false** (standalone) |
| `DCA_SNIPER_TOKEN` / `EXIT_WS_INTERNAL_TOKEN` | auth for execute |

## Redis keys / channels

| Key / channel | Use |
|---------------|-----|
| `aria:dca_sniper:state` | focus + decisions JSON |
| `aria:health:dca_sniper` | heartbeat |
| `aria:dca_sniper:watch` | WS subscribe list |
| `aria:dca_sniper:wake` | pub/sub wake |
| `aria:dca_sniper:events` | decision fan-out |
| `aria:price:*` | price cache updates from WS |

## Local

```bash
export DCA_SNIPER_ENABLED=1
export DCA_SNIPER_TOKEN=dev
export DCA_SNIPER_BOT_URL=http://127.0.0.1:5000
export REDIS_URL=redis://127.0.0.1:6379/0
python -m services.dca_sniper
# GET :5105/health  POST /wake  POST /cycle
```

## Kill

- Stop Railway service, or `DCA_SNIPER_ENABLED=0`
- Bot without sniper: `dca_sniper.enabled=false` restores cycle DCA
