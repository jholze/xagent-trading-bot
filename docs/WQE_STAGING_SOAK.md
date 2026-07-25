# WQE Staging Soak Runbook (R10)

> Epic #124 · Ticket #149  
> Goal: shadow → soft go/no-go before enforce on Railway staging.

## Preconditions

- [ ] Epic branch merged to `staging` (or deploy epic branch) — **mode defaults `off`**
- [ ] `/mode` shows staging build
- [ ] Telegram `/wqe` responds
- [ ] Rollback known: set `watchlist_quality.mode` → `off` and redeploy/reload

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
