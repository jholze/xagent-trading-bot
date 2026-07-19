# D7 Scheduled (calendar / weekly-split) DCA — #102

> **Status:** Implemented (default **OFF**) · no deploy required  
> **Parent:** #79  
> **Code:** `strategies/dca_scheduled.py`

## Behavior

When `dca.scheduled.enabled=true`:

1. Check schedule due (`interval_days`, optional `weekday`)
2. Split `total_usdt` across open-position symbols (`split_usdt_budget`)
3. For each symbol without a dip-DCA candidate, build `BUY_DCA` via `evaluate_scheduled_dca_addon`
4. Optional: apply existing `dca.policy` + cap `spendable_dca`
5. Candidates enter portfolio plan / decision engine as source `dca_scheduled` (lower priority than dip)

When **disabled** (default): code path is never used; dip/recovery unchanged.

## Config (safe defaults)

```json
"dca": {
  "scheduled": {
    "enabled": false,
    "mode": "shadow",
    "interval_days": 7,
    "weekday": null,
    "total_usdt": 500,
    "min_usdt_per_symbol": 50,
    "max_symbols": 10,
    "require_open_position": true,
    "apply_policy": true,
    "respect_spendable_dca": true,
    "only_when_dip_ineligible": true,
    "source_tag": "dca_scheduled"
  }
}
```

## Integration points

| Location | Role |
|----------|------|
| `strategies/dca_scheduled.py` | Pure due/split + candidate builder |
| `strategies/dca_portfolio.collect_dca_targets` | Merge scheduled when dip None |
| `strategies/decision_engine` | Per-coin fallback after dip None |
| Risk/Exec | Unchanged — still BUY_DCA through Risk |

## State

`position.last_scheduled_dca_at` (optional ISO) used for due-check; if missing, schedule is due when interval/weekday rules allow.
