# WQE Staging Soak Runbook (R10)

> Epic #124 · Ticket #149  
> Goal: shadow → soft go/no-go before enforce on Railway staging.

## Preconditions

- [ ] Epic branch merged to `staging` (or deploy epic branch) — **mode defaults `off`**
- [ ] `/mode` shows staging build
- [ ] Telegram `/wqe` responds
- [ ] Rollback known: set `watchlist_quality.mode` → `off` and redeploy/reload

## Data collection (logging)

| Source | Path / command | Contents | Survives redeploy? |
|--------|----------------|----------|--------------------|
| **Event log (primary)** | `logs/wqe_events.jsonl` | `wqe_sync`, `wqe_coin`, `wqe_buy_block`, `wqe_soft_apply` | **Yes** on staging (volume `/app/logs`) |
| **Cycle summary (R15)** | `logs/cycle_summary.jsonl` | `cycle_summary`, `boot_fingerprint` | **Yes** (same volume) |
| **Risk rejects (R15)** | `logs/risk_rejects.jsonl` | every BUY `RiskDecision` reject (`code`, source, WQE scores) | **Yes** |
| **Score snapshot** | `logs/watchlist_quality_scores*.json` | last full score set | **Yes** on staging (same volume) |
| **Human logs** | `logs/aria_log.txt` + Railway stdout | `wqe_event …` / `cycle_summary …` / `risk_reject …` / `WQE boot …` | stdout yes; file yes on volume |
| **Telegram** | `/wqe` · `/wqe soak` | live summary + counters + paths + last cycle age | n/a |

### Railway volume (staging `xagent-test`)

- **Volume:** `xagent-test-volume`  
- **Mount:** `/app/logs` (WORKDIR is `/app` → same as relative `logs/`)  
- **Size:** 50 GB (default)  
- WQE scores + soak JSONLs write under `LOG_DIR` / `WQE_DATA_DIR` so they land on the volume  

Event log is **on by default** whenever `mode` is shadow/soft/enforce (`event_log: true`).  
Disable with `"event_log": false` if needed.

R15 soak streams (`cycle_summary_log`, `risk_reject_log`) default **on** when mode ≠ `off`.  
Env overrides: `WQE_CYCLE_SUMMARY=0|1`, `WQE_RISK_REJECT_LOG=0|1`, `WATCHLIST_QUALITY_EVENT_LOG=0|1`.

### Useful jq (local / after log pull)

```bash
# sync summaries
grep '"type": "wqe_sync"' logs/wqe_events.jsonl | tail -5

# coins demoted by AI
grep '"type": "wqe_coin"' logs/wqe_events.jsonl | grep '"stance": "demote"' | tail -20

# buy blocks (WQE-specific)
grep '"type": "wqe_buy_block"' logs/wqe_events.jsonl | tail -20

# soft drops over time
grep '"type": "wqe_soft_apply"' logs/wqe_events.jsonl | tail -10

# per-cycle health (R15)
grep '"type": "cycle_summary"' logs/cycle_summary.jsonl | tail -10

# boot proof after deploy
grep '"type": "boot_fingerprint"' logs/cycle_summary.jsonl | tail -3

# all BUY risk rejects by code
grep '"type": "risk_reject"' logs/risk_rejects.jsonl | tail -30
grep '"code": "market_block"' logs/risk_rejects.jsonl | wc -l
grep '"code": "watchlist_quality"' logs/risk_rejects.jsonl | tail -10
```

## Phase A — Shadow 48h

```json
"watchlist_quality": { "mode": "shadow", "ai": { "enabled": true, "background_ai": false } }
```

Daily checks:

| Metric | How | Target |
|--------|-----|--------|
| Scores present | `/wqe` n_scored > 0 | yes |
| Score age | `/wqe` / soak | < 1h typical |
| AI success | `/wqe soak` | >70% if AI on |
| Bot health | `/health` | OK |
| No empty watchlist | `/list` | n > 0 |

## Phase B — Soft 48–72h

```json
"watchlist_quality": {
  "mode": "soft",
  "vol_floors": { "t1_min_quote_vol_usd": 750000 },
  "ai": { "enabled": true, "sort_by": "quality_shadow_ai" }
}
```

Daily checks:

| Metric | Target |
|--------|--------|
| Empty list incidents | 0 |
| Avg quote vol of scan set | ↑ vs baseline |
| Unexpected buy blocks | log `WQE block` review |
| Open positions still listed | yes |

## Phase C — Enforce go/no-go

Only if A+B green:

```json
"watchlist_quality": { "mode": "enforce", "min_buy_score": 0.40, "drop_t3": true }
```

Watch: CMC-only + sensor entries, capacity pressure, false blocks on liquid names.

## Rollback

1. `mode: "off"`  
2. Reload config / redeploy  
3. Confirm `/wqe` shows mode=off and `/list` full  

## Log outcomes

Comment on epic #124 with dates + metrics after each phase.
