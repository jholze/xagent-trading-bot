# Market Oracle Service — Railway Test

> **Status:** implemented (MVP)  
> **Service:** `xagent-market-oracle`  
> **Ingest:** `POST /api/market-oracle/ingest`  
> **Related:** `arena-market-oracle-service.md`, Santiment fusion  

## Role

Public market data (Gate BTC/ETH 24h + 4h trend) → global state  
`RISK_ON | NEUTRAL | RISK_OFF | CRASH` → bot via HTTP push.

Bot fuses with Santiment (`market_policy_fusion`: min size, worse regime/sensor).

## Env (oracle service)

| Var | Value |
|-----|--------|
| `BOT_INGEST_URL` | `https://xagent-test-test.up.railway.app/api/market-oracle/ingest` |
| `BOT_INGEST_TOKEN` / `MARKET_ORACLE_INGEST_TOKEN` | shared secret |
| `POLL_INTERVAL_SEC` | 300 |
| `RUN_MARKET_ORACLE` | 1 (or service name match) |

## Bot warm-up

`architecture.market_oracle_warmup_sec` default **1800** (30 min after process start, no new buys).
