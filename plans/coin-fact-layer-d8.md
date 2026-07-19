**Parent epic:** #79 DCA Agent · **Ticket:** [#103](https://github.com/jholze/xagent-trading-bot/issues/103) D8  
**Related:** #72 Memory/RAG · #6 social (closed) · #86/#87 Gate (later) · #98 dca_decision · #99 LIVE_DCA_POLICY  
**Status:** Implemented (default **off**) · plan: `plans/coin-fact-layer-d8-implementation.md`  
**Code:** `intelligence/memory/coin_facts*.py` · policy/context hooks · Hermes `run_memory_cycle`  
**MCP / Agent Hub:** epic [#107](https://github.com/jholze/xagent-trading-bot/issues/107) (split out of D8)
**Follow-up:** [#105 D8b](https://github.com/jholze/xagent-trading-bot/issues/105) — **CMC Pro API** (`coin_facts_cmc_pro.py`) as structured quotes/content provider; HTML `cmc_ai` remains optional.  
**Not this ticket:** #102 scheduled DCA stays `enabled: false`.

## Why

Dip-DCA policy (D1–D6) uses fusion, cash_mode, profile, tech — **not coin-specific news/facts**.

**Staging case (ALLO/USDT, week of 2026-07-13 to 18):**
- Buy sensor 16.07 ~0.40 + DCA 16.07 ~0.38 → full exit 18.07 ~0.44 (**about +429 USDT**)
- External facts that week: AI-token rotation pump (~13.07), then about −10% cool-down / profit-taking + low float/unlock narrative (15.07), sentiment re-bullish chatter (18.07)
- Bot memory: **0 ALLO-scoped memory_market_events** — move was pure TA/exit rules, no structured fact trail
- **CMC AI page** already summarized those facts in structured TLDRs:  
  `https://coinmarketcap.com/cmc-ai/allora/latest-updates/`

Goal: a **shared coin-fact layer for all coins in scope** (open portfolio + watchlist universe), feeding **DCA policy** and **watchlist** — not calendar DCA (#102).

## Vision

```text
Coin universe = open positions ∪ active watchlist (all symbols, not one-off ALLO)
        |
        v
Primary: CMC AI suite per slug (latest-updates + price-analysis)
  (+ later: CMC Pro, LC, unlocks, Gate listings → same event schema)
        |
        v
  CoinFactEvent  ->  memory_market_events (+ optional RAG)
        |
        +--> Watchlist / sensor weight / alerts
        +--> DcaContext + DcaPolicy  (mult / skip / reason_codes)
        +--> /ask (facts in RETRIEVED_MEMORY)
```

**General rule:** every coin we trade or watch is eligible for fact ingest — not a special case for one narrative coin.

## Primary source: CMC AI suite (all coins)

CMC AI exposes a **per-slug family** of pages. We use **three** as first-class inputs (same universe, same pipeline).

### Endpoints (v1 locked)

| Role | URL | What we extract |
|------|-----|-----------------|
| **News / narrative** | `…/cmc-ai/{slug}/latest-updates/` | Dated TLDR bullets, “what this means”, social summary, short-horizon catalysts |
| **Price structure** | `…/cmc-ai/{slug}/price-analysis/` | 24h move vs BTC, **volume spike**, levels, “no catalyst”, short-term outlook |
| **Structural drivers** | `…/cmc-ai/{slug}/price-prediction/` | Medium-term **bullish/bearish impact** sections (utility, unlocks, sector sentiment) — **not** “price will be $X” as a trade signal |
| Optional later | `…/what-is/` | Static explainer; low refresh priority |

Full URL base: `https://coinmarketcap.com/cmc-ai/{slug}/…`

**Examples (ALLO):**
- https://coinmarketcap.com/cmc-ai/allora/latest-updates/
- https://coinmarketcap.com/cmc-ai/allora/price-analysis/
- https://coinmarketcap.com/cmc-ai/allora/price-prediction/

**Disclaimer on all:** *CMC AI can make mistakes* → never sole hard authority.

### Why three pages (complementary)

| latest-updates | price-analysis | price-prediction |
|----------------|----------------|------------------|
| **Why now** (dated news) | **How it trades today** (vol, levels) | **Structural tug-of-war** (utility vs unlocks vs narrative) |
| ALLO: 15.07 −10% cool-down | ALLO: +15% vol breakout, flow-only | ALLO: Quack/Kalshi utility **bullish** vs **20% float / vesting bearish** vs AI rotation **mixed** |
| Short horizon | Intraday / few days | Weeks–months framing |

**Critical rule for prediction page:**  
Use **impact sections** (bullish/bearish drivers), **not** numeric price targets or “2026 price will be …”.  
Those targets are noisy and must **never** alone allow DCA or size-up.

Together they answer: *news? flow? structural headwind?* — all needed for smart DCA.

### ALLO price-prediction snapshot (2026-07-18 CMC AI)

Useful extractable facts (no price target used for trading):
1. **Bullish:** real utility (Quack AI, Kalshi) → recurring demand for inference  
2. **Bearish:** only ~20% circulating, large backer/team vesting → unlock sell pressure  
3. **Mixed:** AI sector rotations → explosive upside and sharp pullbacks  

That is exactly “facts for policy”, not fortune-telling.

### Why use it generally

- **Coin-scoped** (exactly what global Oracle/Santiment lack)
- Structured TLDR + levels map cleanly → taxonomy
- Same URL pattern for **every** CMC slug in portfolio ∪ watchlist
- Complements **CMC Pro API** (quotes/trending) — AI pages = narrative + structure; Pro = raw market numbers

### Access strategy (preferred order)

| Priority | Method | Notes |
|----------|--------|--------|
| **1** | **CMC Pro REST** (#105) | Stable JSON; staging |
| **2** | HTML cmc-ai pages (optional) | Fail-open |
| — | MCP / Agent Hub | **#107** (separate epic) |
| **2** | Controlled fetch of **three** pages (updates + analysis + prediction) | Fail-open; rate-limited; parse TLDR / drivers / levels only |
| **3** | Degrade | Skip coin this cycle; DCA/watchlist fail-open |

Budget: 3 pages × N coins — keep `max_coins_per_cycle` tight (e.g. 40–60); cache by slug+endpoint+TTL; prediction TTL can be longer (slower-changing).

**Not** a substitute for `CMC_API_KEY` Pro endpoints already in the bot.

### Universe & performance (all coins, but bounded)

Ingest is **general** (any coin in scope), but **not** “entire CMC universe”:

| Set | Include |
|-----|---------|
| A | All **open positions** (portfolio) |
| B | Active **watchlist** / sensor watch / trending overlay |
| Cap | Config e.g. `max_coins_per_cycle` (default 40–80), priority A then B |
| Interval | e.g. every 1–6h per coin (TTL/cache); do not scrape every eval tick |
| Concurrency | small pool; backoff on 429 |

Slug resolution: Ticker/pair → CMC slug (reuse existing CMC info/map helpers where possible).

### Parse → CoinFactEvent (v1)

#### From `latest-updates` (prefer dated TLDR items)

| CMC AI signal | event_type | polarity hint |
|---------------|------------|---------------|
| “drops X%”, profit-taking, cools | `profit_taking_narrative` | − |
| unlock / low circulating supply | `unlock` / supply note | − |
| partnership, integration, mainnet | `partnership` / `mainnet` | + |
| hack / exploit / security | `sec_alert` | − hard |
| social “bullish signs” only | `social_spike` | mixed (cap) |
| AI sector rotation | `sector_rotation` | +/neutral |

#### From `price-analysis` (structure / flow)

| CMC AI signal | event_type | polarity hint | DCA use |
|---------------|------------|---------------|---------|
| Volume surge + price up (e.g. vol +174%) | `volume_breakout` | + | soft mult↑ only if not hard-negative news |
| “No clear secondary driver / no news catalyst” | `flow_only_move` | neutral/caution | **no** aggressive DCA; fragile rally |
| Outlook “cautiously bullish” + key support | `structure_bias` | +/mixed | metadata levels; not sole allow |
| Break below support / volume fade risk | `structure_risk` | − | mult↓ |
| Outperformance vs BTC stated | `relative_strength` | + | context for watchlist rank |

Optional metadata from price-analysis: `price`, `change_24h_pct`, `volume_24h`, `volume_change_pct`, `support`, `resistance`, `outlook` (bullish/bearish/cautious).

#### From `price-prediction` (structural drivers only)

| CMC AI signal | event_type | polarity | DCA use |
|---------------|------------|----------|---------|
| Expanding utility / integrations (Quack, Kalshi, …) | `utility_adoption` | + | soft context; mult↑ only with oversold gates |
| Supply unlocks / low float / vesting overhang | `unlock` / `supply_overhang` | − | mult↓ or skip if large (same as news unlock) |
| Sector narrative / AI rotation dependency | `sector_rotation` | mixed | cap size; expect volatility |
| Explicit numeric “price will hit $X by …” | **do not store as trade signal** | — | optional metadata only; **never** policy allow |

`source=cmc_ai_prediction`. Prefer sections labeled Bullish/Bearish/Mixed Impact.

Fields common: `symbols[]`, `event_type`, `impact_score` (−1…+1), `description`, `source` (`cmc_ai_updates` \| `cmc_ai_price` \| `cmc_ai_prediction`), `url`, `as_of`, `ttl_hours`, `metadata.slug`.

**Noise filter:** ignore pure pump-Telegram; CMC AI TLDR primary; community quotes secondary and capped.  
**Conflict rules:**  
1. hard-negative (hack/delist) **beats** any bullish page  
2. unlock/supply_overhang **beats** soft utility_adoption for DCA size  
3. price-prediction **never** alone triggers DCA allow

## Principles (non-negotiable)

1. **Policy-first** — facts → scores/flags; **no Grok in DCA hot path**
2. **Shared layer** — one ingest for portfolio + watchlist (DRY)
3. **All coins in scope** — same pipeline for every symbol in A∪B
4. **Fail-open** — missing CMC AI / parse fail → DCA unchanged
5. **Memory only** — never write orders/ledger
6. **Quality over spam** — CMC AI + official calendars > random social
7. **Symbol-scoped** — every event carries `symbols[]` (e.g. ALLO/USDT)

## Scope

### In

- Spec + schema (MarketEvent / CoinFactEvent taxonomy)
- **CMC AI suite ingest for all scoped coins:** latest-updates + price-analysis + price-prediction (primary source)
- Persist memory_market_events (+ optional RAG)
- Wire `build_dca_context` / `evaluate_dca_policy` with `fact_*` reason_codes
- Watchlist consumption (phase C if large)
- Unit tests + staging soak on multi-coin set

### Out

- #102 weekly scheduled DCA
- Auto-tuning policy mults via Grok
- Full Gate agent (#86/#87) as sole source (can publish into layer later)
- Scraping entire CMC site / unlimited firehose

## Event taxonomy (v1)

| event_type | polarity | DCA use |
|------------|----------|---------|
| listing / delisting | + / − | mult up short window / skip |
| unlock / supply_unlock | − | mult down or skip if large |
| partnership / mainnet | + | soft mult up if oversold |
| hack / exploit / sec_alert | − | **skip hard** |
| ai_narrative / sector_rotation | +/neutral | context or small mult |
| social_spike | mixed | cap mult; never sole buy reason |
| profit_taking_narrative | − | after pump: DCA cautious |
| volume_breakout | + | soft mult only with gates |
| flow_only_move | caution | no aggressive DCA |
| structure_bias / structure_risk | +/− | levels in metadata; mult tweak |
| relative_strength | + | watchlist rank boost |
| utility_adoption | + | soft mult only with gates |
| supply_overhang | − | same family as unlock |

## DCA policy hooks (v1 draft)

| Fact signal | Effect | reason_code |
|-------------|--------|-------------|
| high-impact negative (hack, delist) | skip | `fact_hard_negative` |
| large unlock within N days | mult ≤ 0.5 or skip | `fact_unlock` |
| fresh positive catalyst + oversold gates | mult × 1.1–1.3 (cap) | `fact_catalyst` |
| only social_spike / pump noise | ignore or mult ≤ 1.0 | `fact_noise_ignore` |
| CMC AI profit-taking / cool-down | mult ≤ 0.7 | `fact_profit_taking` |
| volume_breakout + not hard-negative | mult × 1.1–1.2 (cap) | `fact_volume_breakout` |
| flow_only_move (rally without catalyst) | mult ≤ 0.8; no boost | `fact_flow_only` |
| structure_risk (lose support) | mult ≤ 0.5 or skip | `fact_structure_risk` |
| utility_adoption (prediction page) | mult × 1.05–1.15 soft | `fact_utility` |
| supply_overhang / vesting (prediction) | mult ≤ 0.5 or skip | `fact_unlock` (shared code) |
| numeric price target only | **ignore for policy** | — |
| no facts | mult 1.0 | fail-open |

Skip beats size (same as cash/harvest).

## Watchlist (same data)

- Rank / boost fresh positive facts
- Downrank unlock/hack / profit-taking
- Optional Telegram: fact on open position X
- Same CMC AI pipeline for every watchlist coin (within cycle cap)

## Acceptance

- [ ] Spec linked from #79 / #103
- [ ] CMC AI path: for coins in portfolio∪watchlist (capped), fetch+parse **latest-updates, price-analysis, price-prediction** → symbol-scoped events (prediction = drivers only, no target trading)
- [ ] Works for **arbitrary** in-scope tickers (not hard-coded ALLO)
- [ ] `build_dca_context` exposes fact flags; policy emits `fact_*` reason_codes
- [ ] Unit tests: negative fact → skip; no facts → unchanged; noise ignored; multi-symbol universe
- [ ] Staging: ≥1 fact event on a moved portfolio coin in a week **or** documented source gap
- [ ] No Grok in evaluate hot path; no ledger writes; rate limits respected

## PR split

| Phase | Work |
|-------|------|
| **A** | Schema + pure policy fact factors + tests (no network) |
| **B** | CMC AI ingest (slug map, fetch all 3 endpoints, parse, persist) for universe A∪B |
| **C** | Watchlist/sensor consumer + ops (interval, cap, cache) |

## Config sketch (v1)

```json
"memory": {
  "coin_facts": {
    "enabled": true,
    "sources": {
      "cmc_ai": {
        "enabled": true,
        "endpoints": {
          "latest_updates": "https://coinmarketcap.com/cmc-ai/{slug}/latest-updates/",
          "price_analysis": "https://coinmarketcap.com/cmc-ai/{slug}/price-analysis/",
          "price_prediction": "https://coinmarketcap.com/cmc-ai/{slug}/price-prediction/"
        },
        "prefer_mcp_or_api": true,
        "scrape_fallback": true,
        "ttl_hours_updates": 48,
        "ttl_hours_price": 12,
        "ttl_hours_prediction": 72,
        "max_coins_per_cycle": 50,
        "interval_sec": 3600,
        "prediction_use_targets_for_policy": false
      }
    },
    "universe": ["open_positions", "watchlist", "trending_overlay"]
  }
}
```

## Non-goals

- Replacing fusion/oracle global regime
- Scheduled weekly DCA (#102)
- Trusting paid pump signal channels
- Hitting CMC AI for every eval tick or every CMC-listed coin

## References

- ALLO CMC AI suite: [latest-updates](https://coinmarketcap.com/cmc-ai/allora/latest-updates/) · [price-analysis](https://coinmarketcap.com/cmc-ai/allora/price-analysis/) · [price-prediction](https://coinmarketcap.com/cmc-ai/allora/price-prediction/)
- ALLO research (2026-07-19): AI rotation → 15.07 −10% narrative → 18.07 exit; memory 0 coin events
- plans/epic-dca-agent.md · plans/dca-policy-v1.md
- Existing CMC Pro usage: data/cmc_*.py, CMC_API_KEY
- CMC AI Agent Hub / MCP (preferred access if available on plan)

## One-liner

**CMC AI suite (updates + analysis + prediction drivers) for every portfolio and watchlist coin → shared memory facts → smarter DCA and ranking — never trade on numeric price targets alone.**
