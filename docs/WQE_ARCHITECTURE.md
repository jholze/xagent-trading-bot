# Watchlist Quality Engine (WQE) — Architecture

> Epic [#124](https://github.com/jholze/xagent-trading-bot/issues/124)  
> Package: `services/watchlist_quality/`  
> Local stack: `docs/LOCAL_STACK.md`

## Purpose

Improve **universe / scan-set quality** so signals and buys run on coins that are liquid, regime-fit, and memory-aware — optionally AI-critiqued in **shadow** before any membership change.

## Modes

| Mode | Scores | Membership / order | AI critic |
|------|--------|--------------------|-----------|
| `off` | no | unchanged | no |
| `shadow` | det + optional AI fuse | **unchanged** (`behavior_change=false`) | optional |
| `soft` | yes | vol floor + score sort (caller applies) | optional soft-sort by `quality_shadow_ai` |
| `enforce` | yes | tiers/caps/buy gates (W4+) | optional |

Config root: `config.json` → `watchlist_quality`.

```json
"watchlist_quality": {
  "mode": "shadow",
  "vol_floors": { "t1_min_quote_vol_usd": 750000 },
  "honor_memory_soft_block": true,
  "memory": { "enabled": true, "prefer_boost": 0.15, "soft_penalty": 0.4 },
  "ai": {
    "enabled": true,
    "mode": "shadow",
    "max_coins_per_cycle": 12,
    "max_adjust": 0.2,
    "require_evidence": true
  }
}
```

## Authority order (locked)

```text
1. Hard floors (Gate listed, 24h quote vol)     — never LLM override
2. Risk / Memory soft_block on entry            — never LLM sole BUY
3. Deterministic WQE score                      — baseline
4. Memory profile (soft_block / prefer)         — in score via W1
5. RAG evidence pack                            — context for critic
6. LLM critic                                   — adjust ±max, stance only
```

- LLM **never** sole BUY / sole universe include  
- LLM **never** blocks sells  
- Fail-open: memory/RAG/LLM down → deterministic path continues  

## Pipeline

```text
candidates
  → score_coin (liq, mom, narrative, memory, regime)
  → build_rag_pack (lessons/trades/events)
  → run_ai_critic (JSON stance/adjust) [budgeted top-N]
  → quality_shadow_ai = clamp(quality + adjust * confidence)
  → persist watchlist_quality_scores.json + log watchlist_quality_sync
  → [soft] apply_soft_watchlist: drop low-vol non-open, sort score desc
```

## Modules

| Module | Role |
|--------|------|
| `memory_bias.py` | W1 `MemoryWqeInput` |
| `scoring.py` | Pure multi-factor score |
| `rag_pack.py` | AI1 evidence pack |
| `ai_critic.py` | AI2 LLM + fuse math |
| `engine.py` | Orchestrator `run_shadow_score` |
| `soft.py` | W3 vol floor + sort |
| `enforce.py` | W4 tiers/caps/buy gate/memory exclude |
| `universe.py` | W5 sensor + CMC-only helpers |
| `runtime.py` | Soft/enforce glue for `load_effective_watchlist` |
| `soak.py` | AI4/W6 metrics + report text |
| `store.py` | Score artifact JSON |
| `config.py` | mode / weights / ai / floors |

## Operator

- Telegram: `/wqe` — mode, top scores, soak line, score age  
- Telegram: `/wqe soak` — AI agreement metrics from last score file  
- Staging soak: [`WQE_STAGING_SOAK.md`](WQE_STAGING_SOAK.md)

## Residual integration (R-track)

| Area | Status |
|------|--------|
| All BUY paths via RiskManager WQE gate | R1 |
| Sensor loop unified | R2 `get_sensor_watch_coins` |
| Tenant score files | R3 `watchlist_quality_scores.{tenant}.json` |
| Manual `/buy` warn/block | R4 |
| Background rescore | R5 |
| Webhook observe + risk gate | R6 |
| Grid/Hermes policy helpers | R7 `policy.py` |
| Venue quote_vol batch | R12 `venue_batch.py` |
| In-process metrics | R9 `metrics.snapshot()` |

## Interaction matrix (R14)

| System | Interaction with WQE |
|--------|----------------------|
| **position_capacity** | Fewer candidates → less open pressure; no code conflict |
| **slot_eviction** | Eviction still for high-conviction entry; if WQE would block buy, entry should fail at risk with `watchlist_quality` (log both) |
| **cash / fusion** | Fusion `size_mult` still sizes; WQE demote is orthogonal (double caution OK) |
| **Memory soft_block** | Risk path still enforces; WQE adds list-level + buy_allowed |
| **Sells / DCA** | Never blocked by WQE |

## Config defaults (R11)

`BotConfig.watchlist_quality_config` merges defaults with **mode=off**. Unknown mode → `off` via `wqe_mode()`.

## Local usage

```bash
bash scripts/local_stack_up.sh --full
# enable mode=shadow in config for live bot scoring after trending sync
bash scripts/local_stack_test.sh --unit --telegram
python3.13 -c "from services.watchlist_quality import run_shadow_score; print(run_shadow_score([...], config={'watchlist_quality':{'mode':'shadow'}}, persist=False, llm_json_fn=lambda *a,**k: {'stance':'keep','adjust':0,'confidence':0}))"
```

## Related plans

- `plans/arena-watchlist-quality-signals.md`
- `plans/epic-watchlist-quality-engine.md`
- `plans/wqe-ai-shadow-critic.md`
