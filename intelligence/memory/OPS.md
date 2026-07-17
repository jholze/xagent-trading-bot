# Trading Memory + Hermes ops

## Services (Railway test)

| Service | Role | Ledger |
|---------|------|--------|
| `xagent-hermes` | rebuild, news, reflect, Weaviate upsert, Hermes learn | **read-only** orders; writes only `memory_*` |
| `xagent-weaviate` | vectors + filter queries | none |
| `xagent-test` | trades; reads CoinProfile cache | normal |

## Env

| Var | Service | Notes |
|-----|---------|--------|
| `MEMORY_ENABLED=1` | hermes, bot | fail-open off when `0` |
| `WEAVIATE_URL` | hermes | e.g. `http://xagent-weaviate.railway.internal:8080` |
| `HERMES_INTERVAL_SEC` | hermes | default 1800 |
| `HERMES_RUN_LEARNING` | hermes | `0` to skip param learning |
| `HERMES_LIVE_EVIDENCE_MODE` | hermes | `observe` \| `soft` \| `dual` |
| `MONGODB_DB=xagent_test` | hermes | shared docs DB; never seed/repair ledger |

## Collections (safe)

- `memory_coin_profiles`, `memory_market_events`, `memory_trades`, `memory_lessons`
- Store **refuses** `orders`, `positions`, `trade_history`

## Export / restore

```bash
# On Hermes or any host with Mongo
python -c "from intelligence.memory.export import export_jsonl; print(export_jsonl())"
# → logs/memory_export/memory_YYYYMMDD_HHMMSS.jsonl
```

Restore: re-insert JSONL lines into the matching `_collection` field. Profiles are recomputed by rebuild from filled orders (read-only).

## CMC + LunarCrush social memory (Epic #42)

| Source | How Hermes gets it |
|--------|--------------------|
| CMC | `load_cmc_posts()` + `memory_social_feed` dual-write from bot `log_cmc_post` |
| LC | `load_lc_signals()` + `memory_social_feed` dual-write from bot `log_lc_signal` |
| Trending | `load_cmc_trending_overlay()` (optional) |

Config: `memory.social` in `config.json`. Kill-switches: `MEMORY_SOCIAL=0`, `MEMORY_SOCIAL_CMC=0`, `MEMORY_SOCIAL_LC=0`.

Health: `last_social` → `cmc_events`, `lc_events`, `cmc_features`, `lc_features`, `joined_trades`.

Quotes-fallback CMC noise is **excluded by default** (`include_quotes_fallback: false`).

Social never sole BUY; never blocks sells; soft_block only from trade history.

## Fail-open

- Weaviate down → Hermes still writes Mongo; bot uses profiles only.
- Memory Mongo fail → size_bias defaults to 1.0; soft_block skipped.
- CMC/LC empty on Hermes → last_social zeros, cycle continues.
- News providers fail → cycle continues; counters show zeros.

## Health

- Hermes: `GET /health` → `weaviate`, `weaviate_ready`, `live_evidence`, `promotion_rate`, `veto_rate`, `last_news`, `last_rebuild`
