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

## Macro calendar + sessions + Polymarket (Epic #53)

| Piece | Module | Hermes | Bot |
|-------|--------|--------|-----|
| Session clock | `intelligence/macro/session_clock.py` | sync | `get_risk_multipliers()` |
| Calendar windows | `intelligence/macro/calendar.py` | fixtures + optional FRED/AV | cache only |
| BTC corr / impact | `intelligence/macro/btc_correlation.py` | offline stats | impact on events |
| Polymarket | `intelligence/macro/polymarket.py` | fixtures or gamma API | cache |
| Snapshot | `memory_macro_snapshot` | publish | read TTL |

Config: `memory.macro`, `memory.sessions`, `memory.polymarket`, `memory.calendar_risk`.  
Kill-switch: `MEMORY_MACRO=0`.  
Risk factors: `calendar_mult`, `session_mult`, `pm_mult` (fail-open 1.0). Sells never blocked.  
Health: `last_macro` on Hermes `/health`.

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

## Large moves → trigger attribution

When a coin in **open positions ∪ watchlist** moves hard:

1. **Screen on 1h candles** (default |Δ last 1h bar| ≥ 4% or vs BTC ≥ 3pp)
2. **Drill 15m** on hits: strongest impulse bar in last ~2h + volume multiple
3. Score nearby memory events (unlocks, social, news, macro/session/PM pressure, coin facts)
4. Write `price_move_attribution` + optional RAG (`metadata.screen_tf`, `fine_impulse_pct`, `triggers[]`)

CMC 24h quotes are **fallback** only when 1h OHLCV fails.  
Config: `memory.move_attribution.*` · Kill: `MEMORY_MOVE_ATTRIBUTION=0`  
Default **`enabled: false`** until local probe is OK; then set `enabled: true`.

Local dry-run (no Mongo write):

```bash
python3 scripts/probe_move_attribution.py --offline
python3 scripts/probe_move_attribution.py --symbols BTC/USDT,ETH/USDT,SOL/USDT
python3 -m pytest tests/unit/test_move_attribution.py -q
# Only after review:
python3 scripts/probe_move_attribution.py --from-watchlist --top 15 --write
```

## Backward enrich: open book + watchlist

Universe = **open full positions first**, then **active watchlist** (capped).

| Layer | What gets written | How |
|-------|-------------------|-----|
| Ledger rebuild | `memory_trades`, `memory_coin_profiles` (bias/soft_block) | Hermes cycle / `rebuild_from_orders` |
| Open lots without closed sells | synthetic open trades + seed profiles | `scripts/enrich_memory_full.py` |
| Coin narrative (unlocks, CMC) | `memory_market_events` (coin_fact) | `sync_coin_facts` / seed scripts |
| Social | CMC/LC events + join to trades | Hermes social sync |
| Macro pressure | calendar/session/PM events | Hermes macro sync |
| RAG | `memory_rag_chunks` (+ Weaviate) | `index_store_into_rag` |

One-shot full backfill (safe for memory_* only):

```bash
# Local with Mongo env
python3 scripts/enrich_memory_full.py --top 40
# Offline (no CMC/RSS)
python3 scripts/enrich_memory_full.py --top 40 --no-network
# Or thinner ledger seed
python3 scripts/seed_memory_from_ledger.py --top 25
```

Hermes continuous path already uses the same universe for coin facts (`coin_fact_universe`).

## Quality eval (P1)

```bash
# Offline fixture hit-rate (no Mongo required)
python3 scripts/eval_memory_retrieval.py
# Live against configured Mongo/Weaviate
python3 scripts/eval_memory_retrieval.py --live --json
```

## RAG embeddings (P2)

- Hot-path `embed_text` defaults to **hash-64** unless `MEMORY_EMBEDDING_BACKEND=minilm`.
- RAG `embed_for_rag` **prefers MiniLM-384** when installed; falls back to hash-384.
- Overrides: `MEMORY_RAG_EMBED=hash|minilm`, `memory.rag.prefer_minilm`.

## Macro pressure events (P3)

When `calendar_mult` / `session_mult` / `pm_mult` ≠ 1.0, Hermes writes hour-bucketed
`macro_pressure` / `session_pressure` / `pm_pressure` events (in addition to scheduled cards).

## Social→trade join (P4)

`join_social_events_to_trades` uses `join_window_hours_delayed` (default **48h**),
normalized base symbols, and stamps `event.metadata.joined_trade_ids`.

## Decision audit shadow (P5)

`memory.rag.enrich_decision_audit` (default true): attaches `memory_shadow.hits` to
decision audit only — never changes trade actions.

## DCA audit (P6)

DCA policy log lines include `size_bias`, `entry_bias`, `dca_lessons=N` (+ optional summary).

## Health

- Hermes: `GET /health` → `weaviate`, `weaviate_ready`, `live_evidence`, `promotion_rate`, `veto_rate`, `last_news`, `last_rebuild`
