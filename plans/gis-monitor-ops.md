# GIS daily monitor — headless ops (no laptop)

## Problem
Laptop is not always on. M0 monitor must run **unattended** on staging.

## Solution: Railway Cron service

| | |
|--|--|
| Service | `xagent-gis-monitor` |
| Schedule | `0 6 * * *` (06:00 UTC daily ≈ 08:00 Berlin summer) |
| Start | `scripts/railway_start.sh` → `python3 scripts/gis_daily_monitor.py … --persist-mongo` |
| Trigger | `RAILWAY_SERVICE_NAME=xagent-gis-monitor` or `RUN_GIS_MONITOR=1` |

### What it does
1. Fetches live Gate tickers (IST snapshot at run time)
2. Joins **yesterday** demo fills from Mongo `orders_v2` (read-only)
3. Writes files under `/tmp/gis_monitor` (ephemeral)
4. **Upserts** full report to Mongo collection **`gis_daily_monitor`** (durable)
5. Logs one line: `GIS_MONITOR_DONE day=… recall=…`

### Env (copy from xagent-test)
- `MONGO_URL`, `MONGODB_DB=xagent_test`
- `DEMO_MODE=1`, `DEMO_LEDGER_BACKEND=mongo`, `DEMO_ALLOW_REMOTE_MONGO=1`
- optional: `GIS_MONITOR_DAY=yesterday`, `GIS_MONITOR_TOP=20`, `GIS_MONITOR_SCOPE=demo`

### Does **not**
- Touch or rewrite `orders_v2` / positions
- Need laptop, volume, or Telegram (optional later)

### Read a report (local)
```bash
# after railway run or from any mongo client
# collection gis_daily_monitor, filter { day_key: "2026-08-04" }
```

### Manual one-shot on Railway
```bash
railway run -s xagent-gis-monitor -- python3 scripts/gis_daily_monitor.py --day yesterday --persist-mongo
```

### Kill
Delete service or set cron empty / disable schedule in Railway UI.

---

## Alternative: GitHub Actions (often easier)

Workflow: `.github/workflows/gis-daily-monitor.yml`

1. Repo → Settings → Secrets → Actions  
   - `MONGO_URL` = same as Railway bot  
   - optional `MONGODB_DB=xagent_test`
2. Schedule: **06:15 UTC** daily + manual “Run workflow”
3. Report: Mongo `gis_daily_monitor` + Actions **Artifacts** (30 days)

Laptop stays off either way.

### Read latest report from Mongo (any machine)

```js
// mongosh / Compass
use xagent_test
db.gis_daily_monitor.find().sort({day_key:-1}).limit(3)
```
