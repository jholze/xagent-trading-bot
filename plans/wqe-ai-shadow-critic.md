# WQE AI Shadow Critic — Memory + RAG + LLM

> **Status:** Design · shadow-first · no enforce yet  
> **Parent:** Epic [#124](https://github.com/jholze/xagent-trading-bot/issues/124) · builds on W1/W2  
> **Related:** `intelligence/memory/*`, `intelligence/llm_client.py`, `services/watchlist_quality/*`  
> **Principle:** KI **erklärt und re-rankt soft** — sie **ersetzt** keine Liquiditäts-Floors und schreibt keine Orders.

---

## 0. Why

Deterministic WQE (vol, score, memory bias) is necessary but dumb about **context**:

- “This name blew up after unlock last time”
- “Sensor entries on thin alts failed three times in this regime”
- “Narrative is crowded / wash-looking volume”

You already have:

| Layer | What it knows |
|-------|----------------|
| **Memory profiles** | soft_block / prefer / size_bias (W1) |
| **RAG** (Mongo/Weaviate) | lessons, trades, events, soft_block text |
| **LLM** (`llm_client` → Grok or Ollama openai_compat) | structured judgment when given evidence |
| **WQE score** (W2) | transparent multi-factor baseline |

**Combine them in shadow first:** produce an AI-enriched score + rationale **without** changing who gets scanned/bought.

---

## 1. Architecture (shadow)

```text
Candidates (effective watchlist / trending set)
        │
        ▼
┌───────────────────────┐
│ A. Deterministic WQE  │  score_coin() — always on in shadow/soft
│    liq/mom/nar/mem/reg│
└───────────┬───────────┘
            │
            ▼
┌───────────────────────┐
│ B. Memory profile     │  get_memory_wqe_input (already in score)
│    + RAG pack         │  top-k lessons/events for symbol + query
└───────────┬───────────┘
            │
            ▼
┌───────────────────────┐
│ C. LLM Critic (opt)   │  structured JSON only, fail-open
│    batch or top-N     │  cost/latency gated
└───────────┬───────────┘
            │
            ▼
  Shadow artifact:
    quality_score
    ai_adjust  ∈ [-0.2, +0.2]   (shadow only)
    ai_stance  keep|demote|boost|avoid_new
    ai_rationale (short DE/EN)
    evidence_ids[]
        │
        ▼
  Log + watchlist_quality_scores.json
  effective list UNCHANGED until promote flag
```

### Authority order (locked)

```text
1. Hard floors (Gate listed, vol)     — never LLM override
2. Risk / soft_block on entry         — never LLM sole BUY
3. Deterministic WQE score            — baseline
4. Memory profile                     — already in score
5. RAG evidence                       — context for LLM + audit
6. LLM critic                         — soft adjust + text only (shadow)
```

**Invariants**

- LLM **never** sole BUY / sole include in universe  
- LLM **never** blocks sells  
- Fail-open: RAG/LLM down → deterministic score only  
- Shadow: `behavior_change=false` until `watchlist_quality.ai.mode=soft|enforce`

---

## 2. RAG pack (per coin)

Query template (example):

```text
{symbol} watchlist quality soft_block gross loss venue thin
sensor entry rebuy cooloff unlock narrative
```

Retrieve (existing retriever / Weaviate fail-open Mongo):

| Type | Limit | Use |
|------|-------|-----|
| profile | 1 | already have |
| lessons | 3 | avoid patterns |
| trades (recent) | 3 | outcomes |
| events (soft_block, unlock, social) | 4 | context |

Compress to **≤ ~1.5k tokens** evidence block for LLM.

Batch: only score **top N by deterministic quality** or **T2/T3 candidates** (not full Gate catalog) to control cost.

---

## 3. LLM critic contract

### Input (structured)

```json
{
  "symbol": "ARIA/USDT",
  "wqe": { "quality_score": 0.62, "scores": {}, "tier_hint": "T2", "flags": [] },
  "memory": { "entry_bias": "neutral", "size_bias": 1.0 },
  "metrics": { "quote_vol_24h": 1.2e6, "cmc_rank": 8, "source": "cmc_trending" },
  "regime": { "size_mult": 0.8, "label": "neutral" },
  "evidence": [ { "type": "lesson", "text": "..." } ]
}
```

### Output (strict JSON)

```json
{
  "stance": "keep|boost|demote|avoid_new",
  "adjust": -0.15,
  "confidence": 0.0,
  "rationale": "max 240 chars",
  "risk_tags": ["thin_venue", "repeat_loss", "narrative_only"]
}
```

Rules for model:

- `adjust` clamped **[-0.20, +0.20]**  
- If evidence empty → `stance=keep`, `adjust=0`, low confidence  
- Prefer **demote/avoid_new** when soft_block history + thin vol  
- Never invent venue numbers not in metrics  

Implementation: `ask_grok_json` / `llm_client` with required keys; Ollama via `LLM_BACKEND=openai_compat`.

---

## 4. Fusion (shadow score)

```text
quality_shadow = clamp(quality_det + ai_adjust * confidence, 0, 1)
```

Logged fields (extend W2 payload):

```json
{
  "quality_score": 0.62,
  "quality_shadow_ai": 0.55,
  "ai": {
    "stance": "demote",
    "adjust": -0.12,
    "confidence": 0.7,
    "rationale": "...",
    "risk_tags": ["repeat_loss"],
    "model": "grok-4|ollama-...",
    "evidence_n": 4
  }
}
```

**Soft later:** sort by `quality_shadow_ai` instead of `quality_score`.  
**Enforce later:** `avoid_new` + hard_exclude only with memory/vol agreement (2-of-3).

---

## 5. Config sketch

```json
"watchlist_quality": {
  "mode": "shadow",
  "ai": {
    "enabled": true,
    "mode": "shadow",
    "backend": "inherit",
    "max_coins_per_cycle": 12,
    "min_det_score_to_call": 0.0,
    "only_tiers_hint": ["T2", "T3"],
    "timeout_sec": 12,
    "max_adjust": 0.2,
    "require_evidence": true,
    "log_rationales": true
  }
}
```

Kill-switches: `WATCHLIST_AI=0`, `ai.enabled=false`, LLM fail → skip AI block.

---

## 6. Cadence & cost

| Cadence | Recommendation |
|---------|----------------|
| Full det score | every trending sync / cycle (cheap) |
| RAG pack | same, local/mongo |
| LLM critic | every sync **or** every N minutes; **top 8–12** coins only |
| Cache | symbol+fingerprint TTL 30–60 min |

Prefer **Ollama local** for high volume shadow; Grok for higher-quality soak samples if budget allows.

---

## 7. Phases (ticket cut)

| ID | Deliverable | Behavior change |
|----|-------------|-----------------|
| **AI0** | Design freeze (this doc) | no |
| **AI1** | RAG pack builder for WQE symbols + unit tests | no |
| **AI2** | LLM critic JSON + clamp + fail-open | no |
| **AI3** | Fuse into shadow payload + logs + scores file | no |
| **AI4** | Staging soak metrics (agreement det vs AI, hit-rate) | no |
| **AI5** | Optional soft: sort by `quality_shadow_ai` behind flag | soft sort only |

Hard vol floors / tier enforce stay on classic W3/W4 track; AI is a **parallel shadow critic** that can later feed soft sort.

---

## 8. Success metrics (shadow 7d)

| Metric | Goal |
|--------|------|
| AI call success rate | >90% when enabled |
| Fail-open rate | no bot crash; det score always present |
| Agreement: AI demote ∩ later loss/block | qualitative lift |
| False demote on liquid winners | track & tune prompt/weights |
| Latency p95 critic path | &lt; cycle budget (e.g. &lt;30s batch) |

---

## 9. Non-goals

- LLM picks the entire universe from Gate catalog  
- Replacing W1 memory profiles  
- Chat UI / Jarvis  
- Auto-promote AI stance to live buys without soak  

---

## 10. Next

1. Review this design.  
2. File GitHub issue(s) AI1–AI3 under epic #124 (or sub-epic).  
3. Implement AI1 RAG pack → AI2 critic → AI3 shadow fuse.  
4. Keep classic W3 soft floors as separate, complementary path.
