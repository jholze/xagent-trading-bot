# Exit Radar Sidecar (Gate WS getrennt vom Bot)

**Ticket:** #190 · **Epic:** #178 · **Branch:** `feat/exit-radar-sidecar`

## Ziel

Gate public WS + Exit-Radar-GUI laufen in einem **eigenen Railway-Container**.  
Der Bot bleibt **einziger** SELL-Executor (Risk / Ledger / TradingService).

## Architektur

```
xagent-exit-radar                    xagent-test (Bot)
─────────────────                    ─────────────────
Gate WS tickers                      cycles / telegram
pure trail eval                      POST /internal/exit-ws/fire
Flask /exit-radar                    try_execute_trail_exit (local)
Mongo: open positions (read)         Mongo: writes + risk
```

## Config / Env

| Key | Bot | Sidecar |
|-----|-----|---------|
| `exit_realtime.enabled` | true | true (same config.json) |
| `exit_realtime.owner` | `sidecar` (after cutover) | n/a (process is sidecar) |
| `EXIT_REALTIME_OWNER` | `sidecar` | `sidecar` |
| `EXIT_WS_INTERNAL_TOKEN` | shared secret | same |
| `EXIT_EXECUTE_URL` | — | `https://<bot>/internal/exit-ws/fire` |
| `RUN_EXIT_RADAR` / service name | — | `1` / `xagent-exit-radar` |
| `MONGO_URL`, `DEMO_*` | as today | same ledger |

Default remains **`owner=bot`** so merge is safe without the new service.

## Railway cutover (staging)

1. Deploy this PR to staging bot (fire API live, hub still in-bot).
2. Create service **xagent-exit-radar** from same repo/image.
3. Sidecar vars: `RUN_EXIT_RADAR=1`, Mongo, `EXIT_EXECUTE_URL`, token.
4. Bot: set `EXIT_WS_INTERNAL_TOKEN`, then `EXIT_REALTIME_OWNER=sidecar`.
5. Verify: sidecar `/health` hub connected; bot log `hub skipped`; fire path works.
6. GUI: public domain on sidecar → `/exit-radar` (bot route can stay as fallback).

## Rollback

- Bot: `EXIT_REALTIME_OWNER=bot` (or unset) → in-process hub again.
- Stop/remove sidecar service.

## Tests

`pytest tests/unit/test_exit_radar_sidecar.py tests/unit/test_exit_realtime_live.py`
