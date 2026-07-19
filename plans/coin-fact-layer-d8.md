**Parent epic:** #79 DCA Agent · **Ticket:** [#103](https://github.com/jholze/xagent-trading-bot/issues/103) D8  
**Related:** #72 Memory/RAG · #6 social (closed) · #86/#87 Gate (later) · #98 dca_decision · #99 LIVE_DCA_POLICY  
**Status:** Spec + primary source locked (CMC AI) · ready to implement

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
Primary: CMC AI latest-updates per slug
  URL: https://coinmarketcap.com/cmc-ai/{slug}/latest-updates/
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

## Primary source: CMC AI (all coins)

### What it is

| Item | Detail |
|------|--------|
| Product | CMC AI “Latest … News Update” per asset |
| URL pattern | `https://coinmarketcap.com/cmc-ai/{slug}/latest-updates/` |
| Example | ALLO → `…/cmc-ai/allora/latest-updates/` |
| Content | Dated TLDR bullets, deep dives, social summary, roadmap/codebase notes |
| Disclaimer | Page states *CMC AI can make mistakes* → never sole hard authority |

### Why use it generally

- **Coin-scoped** narrative (exactly what global Oracle/Santiment lack)
- Structured enough to map → taxonomy (date + headline + polarity language)
- Same pattern for **every** CMC-listed slug we care about (portfolio + watchlist)
- Complements existing **CMC Pro API** (quotes/trending) — AI page is **narrative**, Pro is **market data**

### Access strategy (preferred order)

| Priority | Method | Notes |
|----------|--------|--------|
| **1** | CMC AI Agent Hub / MCP / official agent API if plan supports | Stable, ToS-friendly |
| **2** | Controlled fetch of latest-updates HTML/markdown | Fail-open; rate-limited; parse TLDR only |
| **3** | Degrade | Skip coin this cycle; DCA/watchlist fail-open |

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

From CMC AI page, prefer **TLDR dated items** first (highest signal/noise):

| CMC AI signal | event_type | polarity hint |
|---------------|------------|---------------|
| “drops X%”, profit-taking, cools | `profit_taking_narrative` | − |
| unlock / low circulating supply | `unlock` / supply note | − |
| partnership, integration, mainnet | `partnership` / `mainnet` | + |
| hack / exploit / security | `sec_alert` | − hard |
| social “bullish signs” only | `social_spike` | mixed (cap) |
| AI sector rotation | `sector_rotation` | +/neutral |

Fields: `symbols[]`, `event_type`, `impact_score` (−1…+1), `description` (TLDR line), `source=cmc_ai`, `url`, `as_of`, `ttl_hours`, `metadata.slug`.

**Noise filter:** ignore pure pump-signal Telegram content; CMC AI TLDR is primary; community quotes secondary and capped.

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
- **CMC AI latest-updates ingest for all scoped coins** (primary source)
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

## DCA policy hooks (v1 draft)

| Fact signal | Effect | reason_code |
|-------------|--------|-------------|
| high-impact negative (hack, delist) | skip | `fact_hard_negative` |
| large unlock within N days | mult ≤ 0.5 or skip | `fact_unlock` |
| fresh positive catalyst + oversold gates | mult × 1.1–1.3 (cap) | `fact_catalyst` |
| only social_spike / pump noise | ignore or mult ≤ 1.0 | `fact_noise_ignore` |
| CMC AI profit-taking / cool-down | mult ≤ 0.7 | `fact_profit_taking` |
| no facts | mult 1.0 | fail-open |

Skip beats size (same as cash/harvest).

## Watchlist (same data)

- Rank / boost fresh positive facts
- Downrank unlock/hack / profit-taking
- Optional Telegram: fact on open position X
- Same CMC AI pipeline for every watchlist coin (within cycle cap)

## Acceptance

- [ ] Spec linked from #79 / #103
- [ ] CMC AI path: for coins in portfolio∪watchlist (capped), fetch+parse latest-updates → symbol-scoped events
- [ ] Works for **arbitrary** in-scope tickers (not hard-coded ALLO)
- [ ] `build_dca_context` exposes fact flags; policy emits `fact_*` reason_codes
- [ ] Unit tests: negative fact → skip; no facts → unchanged; noise ignored; multi-symbol universe
- [ ] Staging: ≥1 fact event on a moved portfolio coin in a week **or** documented source gap
- [ ] No Grok in evaluate hot path; no ledger writes; rate limits respected

## PR split

| Phase | Work |
|-------|------|
| **A** | Schema + pure policy fact factors + tests (no network) |
| **B** | CMC AI ingest (slug map, fetch, TLDR parse, persist) for universe A∪B |
| **C** | Watchlist/sensor consumer + ops (interval, cap, cache) |

## Config sketch (v1)

```json
"memory": {
  "coin_facts": {
    "enabled": true,
    "sources": {
      "cmc_ai": {
        "enabled": true,
        "url_template": "https://coinmarketcap.com/cmc-ai/{slug}/latest-updates/",
        "prefer_mcp_or_api": true,
        "scrape_fallback": true,
        "ttl_hours": 48,
        "max_coins_per_cycle": 60,
        "interval_sec": 3600
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

- ALLO CMC AI page: https://coinmarketcap.com/cmc-ai/allora/latest-updates/
- ALLO research (2026-07-19): AI rotation → 15.07 −10% narrative → 18.07 exit; memory 0 coin events
- plans/epic-dca-agent.md · plans/dca-policy-v1.md
- Existing CMC Pro usage: data/cmc_*.py, CMC_API_KEY
- CMC AI Agent Hub / MCP (preferred access if available on plan)

## One-liner

**CMC AI latest-updates for every portfolio and watchlist coin → shared memory facts → smarter DCA and ranking — general pipeline, not a one-coin hack.**
