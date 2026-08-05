# Gainer Signal Service (WS-1) + Bot consume (WS-2)

Epic #203 · Issues #204 / #205

## Local run

```bash
# Terminal A — signal service (REST seed + WS + optional push)
export GAINER_SIGNAL_TOKEN=dev-secret
export GAINER_SIGNAL_PUSH=0   # set 1 when bot is up
export GAINER_SIGNAL_BOT_URL=http://127.0.0.1:5000
export PORT=5101
python3 -m services.gainer_signal

# Health / leaders
curl -sS http://127.0.0.1:5101/health | python3 -m json.tool
curl -sS 'http://127.0.0.1:5101/leaders?limit=20' | python3 -m json.tool | head

# Terminal B — bot (existing) with same token
export GAINER_SIGNAL_TOKEN=dev-secret
export EXIT_WS_INTERNAL_TOKEN=dev-secret   # also accepted
# start aria_bot as usual

# Manual test consume (demo path)
curl -sS -X POST http://127.0.0.1:5000/internal/gainer-signal \
  -H 'Content-Type: application/json' \
  -H 'X-Gainer-Signal-Token: dev-secret' \
  -d '{"symbol":"BLESS/USDT","last":0.02,"quote_vol":5000000,"rank":3,"pct_24h":25,"eligible":true,"trigger":"heat","source":"gainer_live_heat"}'
```

## Staging

1. Deploy service `xagent-gainer-signal` (root: repo, start: `python -m services.gainer_signal`).
2. Env on service: `GAINER_SIGNAL_TOKEN`, `GAINER_SIGNAL_BOT_URL=https://xagent-test-test.up.railway.app`, `GAINER_SIGNAL_PUSH=1`.
3. Env on bot: same `GAINER_SIGNAL_TOKEN` (or shared `EXIT_WS_INTERNAL_TOKEN`), `gainer_entry.enabled=true` in config.
4. Legacy `gainer_universe` stays on (balloon).

## Kill

- Bot: `gainer_entry.enabled=false` or `GAINER_ENTRY_ENABLED=0`
- Service: stop / `GAINER_SIGNAL_PUSH=0`

## Caps (WS-2)

- **max_open=3**: counts open lots with `entry_source` / source under `gainer_*` (PortfolioService tags `entry_source` on buy for gainer sources; `list_active_positions` exposes it).
- **max_buys_per_day=6**: `max(process-local counter, OrderService.list_day_filled_all gainer buys)` so restarts still see ledger day fills.
- Both enforced in `process_gainer_signal` before any execute.
