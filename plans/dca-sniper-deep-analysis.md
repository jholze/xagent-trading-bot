# Plan: DCA Sniper Deep Analysis (Memory + Policy fidelity)

**Ticket:** [#236](https://github.com/jholze/xagent-trading-bot/issues/236)  
**Branch:** `feat/dca-sniper-deep-memory`  
**Status:** Implemented + quality gates (tests green) — **no deploy** until operator review  
**Date:** 2026-08-09  

---

## 1. Problem (honest)

Plan/tickets (#222, #225, `plans/dca-sniper-service.md`) required:

> full checklist · Memory · Social · Facts · slow deep path · then decide

Shipped sniper v1:

- Service, focus, reclaim, size, recovery_hold ✅  
- Memory layer ≈ `entry_bias` stub ❌  
- Almost no RAG / coin_facts / policy skip ❌  
- No DecisionEvent write-back ❌  

This ticket closes that gap by **reusing #79** (`build_dca_context` + `evaluate_dca_policy` + `persist_dca_decision_event`), not inventing a second memory stack.

---

## 2. Target flow

```text
candidates (bot snapshot: marks, structure flags, RSI/ATR)
        │
        ▼
  [shallow pre-filter optional: grid exclude, dd band]
        │
        ▼
  DEEP PASS  (per remaining candidate; fail-open I/O)
        │  1. build_dca_context(symbol, pos, loss, score_seed, include_rag)
        │  2. enrich candidate: entry_bias, unlock/facts, rag_hits, lessons
        │  3. analyze_candidate → full checklist score
        │  4. evaluate_dca_policy(ctx with final score) → skip | mult
        │  5. if skip → usdt=0, hard_fail += policy_skip
        │  6. else _size_for_row → apply_policy_to_usdt(mult)
        │  7. emit_dca_policy_audit + persist event (source=dca_sniper)
        ▼
  rank / select_focus_batch (unchanged)
        ▼
  cash plan → execute (only if usdt>0 and no hard_fail)
```

**Invariant:** no focus execute without deep pass when `deep_analysis_enabled=true` (default).

---

## 3. Module design

| Module | Role |
|--------|------|
| `services/dca_sniper/deep_analysis.py` | Orchestrate context → checklist → policy → size audit |
| `checklist.py` | Enrich memory/facts layers from real cand fields |
| `engine.py` | Call deep path in `_as_candidate_views` |
| `config.py` / `config.json` | Kill + RAG + policy apply flags |
| `tests/unit/test_dca_sniper_deep_analysis.py` | Prove behavior without live Weaviate/Mongo |

### Config (`dca_sniper.*`)

| Key | Default | Meaning |
|-----|---------|---------|
| `deep_analysis_enabled` | `true` | Master kill → shallow path |
| `deep_include_rag` | `true` | `build_dca_context(include_rag=…)` |
| `deep_apply_policy` | `true` | Policy skip/mult **apply** (not shadow) |
| `deep_analysis_cooldown_sec` | `300` | (reserved; cache optional later) |

---

## 4. Acceptance tests (must pass for “runs as designed”)

1. **Context soft_block** → memory score down + policy mult ≤ soft_block path  
2. **fact_hard_negative / unlock** → facts hard fail and/or policy skip → **usdt=0**  
3. **policy harvest_skip** → no focus size  
4. **policy deploy mult** → usdt scaled up vs base small  
5. **deep_analysis_enabled=false** → no context builder call (shallow)  
6. **Audit payload** contains `context` keys + `policy_reasons` on view checklist  
7. Existing pure/engine dry tests still green  

---

## 5. Non-goals (this PR)

- Deploy / Railway  
- Grok in path  
- Full multi-TF rewrite  
- Fund-from-winner memory gate (reuse later)  
- Closing #79 (separate; children already shipped)

---

## 6. Kill / rollback

`dca_sniper.deep_analysis_enabled=false` → previous shallow scoring only.  
No schema migration.

---

## 7. Definition of done (operator)

- Plan file + #236  
- Code + unit proof above  
- Short demo script or unittest output showing “context filled → decision”  
- **No deploy** until you say so  

---

## 8. Quality gates (follow-up hardening)

| Gate | Behavior |
|------|----------|
| `context_quality` | Counts profile/rag/lessons/facts/structure/ta/funding/cash_mode + news/path/wallet |
| `deep_min_context_signals` | default 3 — below = **thin** |
| `deep_require_context_for_heavy` | thin ⇒ demote **HEAVY → SMALL** (or block) |
| multi-TF structure | 15m/1h/4h aggregate free_fall/reclaim |
| metrics | `deep_passes`, `deep_thin`, `deep_rich`, `deep_rag_hits`, `deep_with_facts`, `policy_skips` |

---

## 9. Evidence layer (news / path / wallet)

| Channel | Strategy | Decision effect |
|---------|----------|-----------------|
| **News/facts** | Read Memory events (ingest async elsewhere); never live-scrape in sniper | hard_news demotes/blocks heavy; unlock/hack → flags + policy |
| **Path stats** | Precomputed recovery quality | high giveback → size ×0.85 |
| **Wallet/on-chain** | Provider adapter; default **unavailable** (no invented whales) | when feed exists: exchange inflow soft mult↓ |

Module: `services/dca_sniper/evidence.py`  
Checklist layer: `news` (hard on hack/unlock-class).

---

## 10. Santiment Pro (full stack utilization)

| Layer | Source | Effect on sniper |
|-------|--------|------------------|
| **Global regime** | Sidecar → Redis `aria:santiment:latest` → `get_santiment_policy` | CRASH/`block_buys` → size 0; RISK_OFF size_mult + social_caution; size_mult scales adds |
| **Per-asset** | SanAPI via `SANTIMENT_API_KEY` (optional on sniper) | DAA/vol/social/flows/MVRV → soft size mult; exchange inflow demotes HEAVY |
| **Lag rule** | Sanbase Pro often ~30d lag on social/funding/flows | Stale → `research_*` half-weight only; never alone hard-block |

Module: `services/dca_sniper/santiment_enrich.py`  
Client: `services/santiment_sidecar/client.py` (`fetch_asset_signals`, lean metric set)  
Wired in: `deep_analysis._enrich_santiment` + `apply_santiment_size` after evidence.

| Config | Default | Notes |
|--------|---------|-------|
| `deep_santiment_enabled` | true | Master for deep path (global Redis) |
| `deep_santiment_asset_fetch` | **false** | Opt-in only — burns SanAPI quota |
| `deep_santiment_asset_ttl_sec` | 21600 | 6h cache if asset fetch on |
| `deep_santiment_micro` | true | 1 metric (DAA) per asset |
| `deep_santiment_block_buys` | true | Honor CRASH global block |

**Sidecar thrift (Pro budget):** poll **1h**, **3 core** metrics (btc/eth DAA + btc vol),
optional social+funding every 6th poll, **no** lag double-fetch. Target ~2–3k calls/mo.

Tests: `tests/unit/test_dca_sniper_santiment.py`  
Quality flag: `has_santiment` in `context_quality`.
