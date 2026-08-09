# DCA Sniper Service (Epic #222)

Fully automatic quality-first heavy DCA recovery. **No human approve.**

## Staging sharp (default in config now)

```json
"dca_sniper": {
  "enabled": true,
  "notify_only": false,
  "mode": "live",
  "in_process_tick": true,
  "require_reclaim_for_dca": true,
  "prefer_small_before_heavy": true,
  "heavy_only_on_reclaim": true
}
```

- **Real BUY_DCA** via Risk + ledger (demo dry_run_enhanced still paper exchange)
- **recovery_hold** set on each sniper fill
- **Cycle portfolio DCA off** while sniper enabled
- Bot ticks sniper in-process each portfolio DCA pass (`in_process_tick`)

Kill: `DCA_SNIPER_ENABLED=0` or `"enabled": false`.  
Log-only emergency: `DCA_SNIPER_NOTIFY_ONLY=1`.

## Optional sidecar

```bash
export DCA_SNIPER_TOKEN=dev-token
export DCA_SNIPER_ENABLED=1
export DCA_SNIPER_BOT_URL=http://127.0.0.1:5000
python -m services.dca_sniper
```

## Bot routes

| Method | Path | Role |
|--------|------|------|
| GET | `/internal/dca-sniper/candidates` | open red bags snapshot |
| GET | `/internal/dca-sniper/cash` | spendable_dca + floor |
| GET | `/internal/dca-sniper/status` | holds + flags |
| POST | `/internal/dca-sniper/execute` | BUY_DCA + recovery_hold |
| POST | `/internal/dca-sniper/fund-sell` | winner → cash |
| POST | `/internal/dca-sniper/promote` | BE+ clear hold |

Token: `X-Dca-Sniper-Token` or `DCA_SNIPER_TOKEN` env.

## Sniper service

| Path | Role |
|------|------|
| GET `/health` | liveness |
| GET `/status` | focus + last audit |
| POST `/wake` | WS/external wake |
| POST `/cycle` | force one cycle |

## Kill

- `DCA_SNIPER_ENABLED=0`
- `RECOVERY_HOLD_ENFORCE=0` (hold sell gates only)

## Spec

`plans/dca-sniper-service.md`
