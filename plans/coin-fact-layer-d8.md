**Parent epic:** #79 DCA Agent  
**Related:** #72 Memory/RAG · #6 social (closed) · #86/#87 Gate (later) · #98 dca_decision · #99 LIVE_DCA_POLICY  
**Status:** Ready for design then implement (preferred over #102 scheduled DCA)

## Why

Dip-DCA policy (D1–D6) uses fusion, cash_mode, profile, tech — **not coin-specific news/facts**.

**Staging case (ALLO/USDT, week of 2026-07-13 to 18):**
- Buy sensor 16.07 ~0.40 + DCA 16.07 ~0.38 → full exit 18.07 ~0.44 (**about +429 USDT**)
- External facts that week: AI-token rotation pump (~13.07), then about −10% cool-down / profit-taking + low float/unlock narrative (15.07), sentiment re-bullish chatter (18.07)
- Bot memory: **0 ALLO-scoped memory_market_events** — move was pure TA/exit rules, no structured fact trail

Goal: a **shared coin-fact layer** for **watchlist** and **DCA policy** (and later /ask), not calendar DCA (#102).

## Vision

```text
Sources (CMC / LC / news / unlocks / listings — phased)
        |
        v
  CoinFactEvent  ->  memory_market_events (+ optional RAG)
        |
        +--> Watchlist / sensor weight / alerts
        +--> DcaContext + DcaPolicy  (mult / skip / reason_codes)
```

## Principles (non-negotiable)

1. **Policy-first** — facts become structured scores/flags; **no Grok in DCA hot path**
2. **Shared layer** — one ingest for portfolio + watchlist (DRY)
3. **Fail-open** — missing facts → current DCA behavior unchanged
4. **Memory only** — never write orders/ledger from this path
5. **Quality over spam** — prefer CMC/official/unlock calendars; de-rank Telegram pump signals
6. **Symbol-scoped** — every event carries symbols[] (e.g. ALLO/USDT)

## Scope

### In

- Spec + schema for CoinFactEvent (or MarketEvent with event_type taxonomy)
- Ingest pipeline (start with 1–2 sources already in stack: CMC/news/social if present)
- Persist to memory_market_events (+ optional RAG like #98)
- Wire read path into build_dca_context / evaluate_dca_policy with fact_* reason_codes
- Document watchlist/sensor consumption (stub OK if large)
- Unit tests: taxonomy, policy mult/skip, fail-open
- Staging soak: ALLO-class coin has at least one fact event during a move week OR documented source gap

### Out

- #102 weekly scheduled DCA
- Auto-tuning policy table via Grok
- Full Gate listings agent (#86/#87) — may publish into this layer later
- Real-time Twitter firehose / unlimited scraping

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

Impact score −1…+1; source + url; as_of / TTL so stale facts expire.

## DCA policy hooks (v1 draft)

| Fact signal | Effect | reason_code |
|-------------|--------|-------------|
| high-impact negative (hack, delist) | skip | fact_hard_negative |
| large unlock within N days | mult ≤ 0.5 or skip | fact_unlock |
| fresh positive catalyst + oversold gates | mult × 1.1–1.3 (cap) | fact_catalyst |
| only social_spike / pump-group noise | ignore or mult ≤ 1.0 | fact_noise_ignore |
| no facts | mult 1.0 | fail-open |

Skip beats size (same as cash/harvest).

## Watchlist (same data)

- Rank / boost fresh positive facts
- Downrank unlock/hack
- Optional Telegram: fact on open position X
- Full wiring may be phase C; DCA read path must not wait on it

## Acceptance

- [ ] Spec in plans/ linked from #79
- [ ] At least one ingest path writes symbol-scoped events to memory
- [ ] build_dca_context exposes fact flags/scores; policy emits fact_* reason_codes
- [ ] Unit tests: negative fact → skip; no facts → unchanged; noise ignored
- [ ] Staging soak note as above
- [ ] No Grok in evaluate hot path; no ledger writes

## PR split

| Phase | Work |
|-------|------|
| A | Schema + pure policy factors + tests (no new network) |
| B | Ingest from existing stack (CMC/news/social in Hermes) |
| C | Watchlist/sensor consumer + ops docs |

## Non-goals

- Replacing fusion/oracle global regime
- Scheduled weekly DCA (#102)
- Trusting paid signal channels as primary source

## References

- ALLO research (2026-07-19 session): AI rotation pump → 15.07 −10% narrative → 18.07 exit; bot memory 0 coin events
- plans/epic-dca-agent.md · plans/dca-policy-v1.md
- intelligence/memory/* · Santiment/Oracle fusion (global only)

## One-liner

One coin-fact memory layer powers watchlist awareness and smarter DCA — facts as policy inputs, not Grok orders.
