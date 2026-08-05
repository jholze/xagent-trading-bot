# GIS Performance Monitoring (Foundation)

**Status:** SPEC — strong monitoring as **baseline for all GIS/WS changes**  
**Parent Epic:** #203 · Child: **WS-5 #208** (elevated: do **before or in parallel with** WS-1, not after)  
**Environment:** Staging/Demo first  
**Related:** `scripts/daily_auswertung.py` (exists, **not** gainer-IST), `order_day_stats`, GIS metrics in `experiment-GIS-14-…`

---

## 1. Why

Without **daily IST vs bot** data we cannot tell if WS-1/WS-2 helped or hurt.  
Monitoring is the **measurement plane**; signal service is the **control plane**.

**Principle:** Every change (signal service, caps, vol 500k, exit purity) is judged against this monitor — not gut feel.

---

## 2. What we need (strong monitoring)

### 2.1 Daily IST snapshot (market truth)

Every UTC day (e.g. 00:15 after day close, + optional mid-day 12:00):

| Field | Source |
|-------|--------|
| day_key | UTC date |
| Gate Leaders Top-20 (and Top-100 optional) | REST tickers rank 24h% Spot USDT **or** service `/leaders` once live |
| per coin: rank, pct_24h, quote_vol, leverage flag | same |
| eligible_now (vol≥500k, no lev) | rule frozen |

**Persist:** Mongo collection e.g. `gis_day_leaders` **and/or**  
`auswertungen/gis/YYYY-MM-DD_leaders.json`

### 2.2 Daily bot reality

From `orders_v2` + open positions (+ later signal-service logs):

| Field | Source |
|-------|--------|
| All filled buys/sells that day | orders_v2 by day_key, tenant, scope=demo |
| By source (grid / dca / gainer_* / …) | source / exit_source |
| Gainer-sleeve: buys, sells, realized pnl | filter source=gainer_* |
| Opens at EOD: n, notional, uPnL | positions / exit-radar snapshot |
| Recognized / eligible (if service up) | gainer-signal API or bot state |
| Caps: max concurrent gainer opens that day | derived |

### 2.3 Daily join (the actual monitor)

For each IST Top-20 coin:

| Column | Meaning |
|--------|---------|
| rank_ist | Gate/service rank |
| vol, eligible | liquid? |
| recognized | bot/service saw it? |
| bought_gainer | fill source=gainer_* that day or open from gainer |
| bought_other | grid/dca bought same symbol |
| missed | eligible + not bought + rank≤K |
| sold_today | sell fill + pnl |
| note | FOMO / low vol / cap full |

**KPIs computed daily:**

| KPI | Formula | Target (from GIS-14) |
|-----|---------|----------------------|
| **Recall@20** | \|recognized ∩ IST Top-20\| / 20 | ≥ 0.90 (once service live; baseline now = bot live_top) |
| **Eligible coverage** | eligible in Top-20 / 20 | report |
| **Sleeve hit-rate** | gainer buys on IST Top-20 eligible / eligible Top-20 | report (low until WS-2) |
| **Missed liquid leaders** | eligible Top-10 not bought (any source) | report |
| **Gainer expectancy** | sum pnl gainer sells / n | ≥ 0 over window |
| **Grid vs gainer pnl** | split by source | report |
| **Cap pressure** | times at max 3 gainer opens | report |

### 2.4 Rolling windows

| Window | Use |
|--------|-----|
| **1d** | daily report |
| **7d** | PASS/LEARN/KILL |
| **14d** | GIS balloon review |

---

## 3. Deliverables

### M0 — Baseline script (first, **before** full signal service)

`scripts/gis_daily_monitor.py`

```bash
# Offline / cron / railway run
python scripts/gis_daily_monitor.py --day yesterday --top 20 --scope demo
# writes:
#   auswertungen/gis/YYYY-MM-DD_monitor.json
#   auswertungen/gis/YYYY-MM-DD_monitor.md
```

**Inputs (phase M0, no new service):**
- Gate REST tickers → IST top-20 (and store)
- Mongo orders_v2 (via env MONGO_URL on staging SSH/cron)
- Optional: load gainer state file / health if available

**Outputs:** JSON + MD scorecard for that day.

### M1 — Schedule

- **Staging:** Railway cron service or external cron → `railway run` / HTTP trigger  
- **Local fallback:** extend `cron_daily_auswertung.sh` / launchd to also call `gis_daily_monitor.py`

### M2 — After WS-1 live

- IST rank from **gainer-signal** `/leaders` (preferred) + Gate REST cross-check  
- Add columns: recognized_by_service, signal_emitted, signal_skipped_cap

### M3 — Ops UX

- Telegram optional (like daily_auswertung `--telegram`) short summary  
- 7d rollup: `gis_weekly_scorecard.py`  
- Kill recommendation line if S1/L1 fail

---

## 4. Relation to existing tools

| Existing | Gap | Action |
|----------|-----|--------|
| `daily_auswertung.py` | PnL/trades/hermes — **no** Gate Leaders IST | keep; **add** GIS monitor alongside |
| `order_day_stats` | day aggregates | reuse for buy/sell counts |
| `backtest_watchlist_rotation --daily-top` | offline sim | not production monitor |
| `gainer_ws_board.jsonl` | partial board | not joined to fills |
| ad-hoc `day_scorecard_*.json` | one-off | replace with systematic `auswertungen/gis/` |

---

## 5. Reorder epic work (foundation first)

```text
WAS:  WS-1 service → WS-2 buy → … → WS-5 monitor
SOLL: M0 monitor baseline → WS-1 → WS-2 → enrich monitor (M2) → cutover → clean
```

| Order | Work | Issue |
|-------|------|--------|
| **0** | **GIS daily monitor M0** | **#208** (expand) |
| 1 | Signal service | #204 |
| 2 | Demo buy max 3 | #205 |
| 3 | Exit purity | #206 |
| 4 | Dual-truth cutover | #207 |
| 5 | Monitor M2+weekly | #208 continued |
| 6 | Stack clean | #209 |

---

## 6. Acceptance (monitoring strong enough)

- [ ] ≥7 consecutive daily JSON/MD files under `auswertungen/gis/` (or Mongo)  
- [ ] Each day has IST Top-20 + join to bot fills  
- [ ] 7d rollup shows Recall (or baseline proxy), sleeve hits, pnl by source  
- [ ] After WS-1: Recall uses service leaders  
- [ ] Operator can answer in &lt;10 min: “Did yesterday’s change help?”  

---

## 7. Example daily MD section

```markdown
# GIS Monitor 2026-08-05

## IST Leaders Top-10
| rank | symbol | pct | vol | eligible | bot_recognized | bought_gainer | bought_other | missed |
...

## Bot day
- fills: buy X sell Y | gainer_buys: n | gainer_sell_pnl: …
- open: n notional uPnL

## KPIs
- Recall@20: … | Sleeve hit eligible Top-10: … | Missed liquid: …

## Verdict line
BASELINE | IMPROVED | REGRESSED vs 7d avg
```

---

## 8. First build slice (concrete)

1. `scripts/gis_daily_monitor.py` — pure-ish + mongo optional  
2. Write `auswertungen/gis/`  
3. Document cron one-liner for staging  
4. Run once on staging (railway ssh) for **yesterday** as baseline **before** WS-1  

### M0 implemented (feat/gis-daily-monitor)

```bash
# Local / offline fixtures
python3 scripts/gis_daily_monitor.py --day yesterday --top 20 --scope demo

# With staging mongo (from machine that resolves MONGO_URL):
MONGO_URL=... MONGODB_DB=xagent_test \
  python3 scripts/gis_daily_monitor.py --day yesterday --top 20 --scope demo

# Railway one-shot (after deploy of this branch):
# railway ssh -s xagent-test -e test -- \
#   python3 scripts/gis_daily_monitor.py --day yesterday --top 20 --scope demo
```

Pure core: `services/gis_monitor/pure.py`  
Tests: `tests/unit/test_gis_daily_monitor.py`

---

*Monitoring is not optional polish — it is the basis for evaluating the epic.*
