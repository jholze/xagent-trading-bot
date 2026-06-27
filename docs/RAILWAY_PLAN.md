# Railway Deployment Plan — X-Agent Trading Bot

**Status:** Planned (not deployed)  
**Last updated:** 2026-06-27  
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

## Deliverables (when executing this plan)

1. `Dockerfile` + `.dockerignore`
2. `railway.toml`
3. `scripts/railway_start.sh`
4. `aria_bot.py` — `PORT` / `0.0.0.0`
5. `services/webhook_watchdog.py` — static Railway URL mode
6. `scripts/mongo_seed_from_local.py` (optional one-time)
7. Update this doc with actual Railway URL + deploy date

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