# Railway Deployment Plan — X-Agent Trading Bot

**Status:** Ready to deploy (Dockerfile + `railway.toml` on `main`)  
**Last updated:** 2026-07-01
**Branch baseline:** `main` after Mongo ledger + Telegram position tree merge

---

## Goal

Run the trading bot 24/7 on [Railway](https://railway.app) with:

- **Persistent MongoDB** (orders, positions, trade history)
- **Telegram webhook** (no ngrok)
- **Safe separation** from local demo (`xagent_test`) and production (`xagent`)

---

## Current local architecture (reference)

| Component | Local today |
|-----------|-------------|
| Process | `aria_bot.py` — Flask webhook + trading loop in one process |
| HTTP | Port `5000`, ngrok tunnel for Telegram |
| Ledger | `architecture.ledger_backend: "mongo"` |
| Demo DB | `MONGODB_DB=xagent_test` + `DEMO_MODE=1` |
| Prod DB guard | `assert_safe_demo_mongo_db()` aborts if demo → `xagent` |
| Local JSON | `*.demo.json` for demo scope (ephemeral on Railway) |

---

## Target Railway architecture

```
Telegram ──HTTPS──► Railway Web Service (aria_bot.py)
                         │
                         ├── MONGODB_URI → MongoDB Atlas (or Railway Mongo)
                         ├── Gate / CMC / xAI APIs
                         └── /health (Railway healthcheck)
```

**One Railway service** (monolith). No Redis required for v1 (optional later).

---

## Pre-deploy checklist (database safety)

Before any cloud deploy:

- [ ] **Never** set `DEMO_MODE=1` on Railway (uses ephemeral JSON files)
- [ ] **Never** point demo local bot at `MONGODB_DB=xagent` without intent
- [ ] Use **`xagent`** (or `xagent_prod`) for Railway production paper/live
- [ ] Keep **`xagent_test`** for local Mac demo only
- [ ] Confirm `assert_safe_demo_mongo_db()` stays in `aria_bot.py` startup
- [ ] Seed Mongo once from local JSON if migrating existing demo state
- [ ] No code path calls `drop_database()` except test/smoke scripts

**Local restart is safe when:**

```bash
export DEMO_MODE=1
export MONGODB_DB=xagent_test
# scripts/restart_demo_scheduled.sh sets both
```

---

## Implementation phases

### Phase 1 — Container & port (required)

| Task | File | Notes |
|------|------|-------|
| Dockerfile | `Dockerfile` | Python 3.13, `libta-lib-dev`, `requirements.txt` |
| Dynamic port | `aria_bot.py` | `PORT = int(os.environ.get("PORT", 5000))`, `host="0.0.0.0"` |
| Production server | `railway.toml` or start cmd | `gunicorn` or threaded Flask (v1: keep Flask) |
| Health check | existing `/health` | Railway → `GET /health` |

### Phase 2 — MongoDB (required)

| Task | Notes |
|------|-------|
| Provision Atlas cluster | Free M0 sufficient for paper |
| Connection string | `MONGODB_URI=mongodb+srv://...` in Railway vars |
| DB name | `MONGODB_DB=xagent` |
| Network | Allow Railway egress (or `0.0.0.0/0` initially) |
| Config | `architecture.ledger_backend: "mongo"`, `ledger_dual_write: false` |
| Migration | `scripts/mongo_migrate_json.py` one-time seed from local orders |

### Phase 3 — Telegram without ngrok (required)

| Task | Notes |
|------|-------|
| Webhook URL | `https://<railway-domain>/` via `setWebhook` on startup |
| Disable ngrok watchdog | New `RAILWAY_PUBLIC_DOMAIN` mode in `webhook_watchdog.py` |
| Startup script | `scripts/railway_start.sh` — register webhook + command menu |
| Chunked messages | Already implemented (`chunk_positions_message`) |

### Phase 4 — Ephemeral disk hardening (required)

Railway filesystem resets on redeploy. **Must use Mongo for:**

- Orders, positions, trade history
- Optional: ask-bridge queue → Mongo collection later

**Do not rely on:**

- `data/*.demo.json`, `logs/`, `run/`, ngrok

### Phase 5 — Config & secrets (required)

Railway environment variables:

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
MONGODB_URI=
MONGODB_DB=xagent
# Optional trading keys:
GATE_API_KEY=
GATE_SECRET=
OPENAI_API_KEY=
CMC_API_KEY=
WEBHOOK_BASE_URL=https://<app>.up.railway.app
```

Keep secrets out of `config.json`; use env overrides in `core/config.py` where needed.

### Phase 6 — Ops & cost (recommended)

| Item | Recommendation |
|------|----------------|
| Plan | Hobby/Pro for 24/7 (free tier sleeps) |
| Memory | 512 MB–1 GB (27+ positions, background backtest) |
| `background_backtest_enabled` | `false` on Railway v1 (save CPU) |
| Logs | stdout → Railway log drain |
| Alerts | Telegram on startup + health failure |

---

## Suggested first Railway mode

**Paper trading + Mongo `xagent` + Telegram live**

- No real Gate orders until `live_confirmed` flow is intentional
- Mirrors enhanced dry-run but cloud-persistent
- Local Mac stays on `xagent_test` for experiments

---

## Deliverables (done)

1. `Dockerfile` + `.dockerignore`
2. `railway.toml`
3. `scripts/railway_start.sh` — demo mode + `DEMO_LEDGER_BACKEND=mongo`
4. `scripts/register_railway_webhook.py`
5. `aria_bot.py` — `PORT` / `HOST=0.0.0.0`
6. `services/webhook_watchdog.py` — `WEBHOOK_BASE_URL` / `RAILWAY_PUBLIC_DOMAIN`
7. `railway.env.example` — copy vars into Railway dashboard

---

## Quick deploy (step by step)

### 1. MongoDB Atlas

- Create cluster (M0 free tier is enough for demo)
- Database: `xagent_test` (same as local demo)
- Network access: allow Railway egress (`0.0.0.0/0` initially)
- Copy `MONGODB_URI`

**One-time seed** from your Mac (optional, if you want existing orders/positions):

```bash
cd /path/to/trading_bot
export MONGODB_URI='mongodb+srv://...'
export MONGODB_DB=xagent_test
python3 scripts/mongo_migrate_json.py --scope demo --test-db
```

### 2. Railway project

```bash
# Install CLI: npm i -g @railway/cli  OR  brew install railway
railway login
cd trading_bot
railway init          # new project or link existing
railway link
```

In Railway dashboard:

1. **Service** → connect GitHub repo `main` branch (or `railway up` from CLI)
2. **Settings** → Networking → **Generate domain** (public HTTPS URL)
3. **Variables** — copy from `railway.env.example`:
   - `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
   - `MONGODB_URI`, `MONGODB_DB=xagent_test`
   - `WEBHOOK_BASE_URL=https://<your-domain>.up.railway.app`
   - API keys from local `.env` (Gate, CMC, OpenAI, X as needed)

### 3. Stop local bot before first deploy

Only **one** Telegram webhook URL per bot token. Stop ngrok/local bot:

```bash
bash scripts/stop_bot.sh
```

### 4. Deploy

```bash
railway up
# or push to main with GitHub integration
```

Check logs for `MongoDB OK` and `Telegram webhook registered`.

### 5. Verify

```bash
curl https://<your-domain>.up.railway.app/health
# → OK

curl "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"
# → url should match your Railway domain
```

Send `/gate` or `/mode` in Telegram.

### Railway mode vs local

| | Local Mac | Railway |
|---|-----------|---------|
| Demo | `DEMO_MODE=1` | same (via `railway_start.sh`) |
| Mongo DB | `xagent_test` | `xagent_test` (recommended) |
| Ledger | `demo_hybrid` (JSON+Mongo) | `mongo` via `DEMO_LEDGER_BACKEND` |
| Webhook | ngrok | `WEBHOOK_BASE_URL` |
| Watchlist | `watchlist.demo.json` | seeded from `watchlist.json` on first boot |

---

## Rollback

- Local Mac: `bash scripts/restart_demo_scheduled.sh` (unchanged)
- Railway: redeploy previous image or `railway rollback`
- Mongo: Atlas point-in-time restore (enable on prod cluster)

---

## Merge note (2026-06-27)

Merged `feature/mongodb` → `main` includes:

- Mongo ledger backend
- Demo cash reconciliation from orders
- Position tree `/positions` + Telegram chunking
- `assert_safe_demo_mongo_db` production guard

Local bot restart after merge uses **`DEMO_MODE=1` + `MONGODB_DB=xagent_test`** — production Mongo untouched.