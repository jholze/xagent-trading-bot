# Trading Memory + Hermes External (Epic #30)

## Services

| Service | Start | Writes |
|---------|-------|--------|
| `xagent-test` | `railway_start.sh` bot | ledger (unchanged) |
| `xagent-hermes` | `RUN_HERMES=1` → `python -m intelligence.memory.service` | **only** `memory_*` collections |
| `xagent-weaviate` | optional image `semitechnologies/weaviate` | vectors |

## Env (Hermes)

| Var | Value |
|-----|--------|
| `RUN_HERMES` | `1` |
| `MONGODB_DB` | `xagent_test` (same as bot — memory collections only) |
| `MONGO_URL` / `MONGODB_URI` | shared |
| `HERMES_INTERVAL_SEC` | `1800` |
| `WEAVIATE_URL` | optional `http://xagent-weaviate.railway.internal:8080` |
| `HERMES_RUN_LEARNING` | `1` to run HermesAgent cycle; `0` for memory-only |

## Ledger safety

- Memory package refuses collections not starting with `memory_`
- Rebuild **reads** orders via `load_orders` — never `save_orders`
- Hermes start path does **not** run seed/repair scripts

## Bot

- `architecture.hermes_external: true` — no in-process Hermes thread
- Risk: `coin_size_bias` from `intelligence.memory.cache`
- Fail-open if memory missing
