# Plan: DCA Sniper Deep Analysis (Memory + Policy fidelity)

**Ticket:** [#236](https://github.com/jholze/xagent-trading-bot/issues/236)  
**Branch:** `feat/dca-sniper-deep-memory`  
**Status:** Implemented locally (tests green) — **no deploy** until operator review  
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
