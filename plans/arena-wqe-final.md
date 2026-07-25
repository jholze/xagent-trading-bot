# Arena Final: Watchlist Quality Engine (Epic #124)

> **Status:** Implementation complete on `epic/wqe-124-watchlist-quality-engine`  
> **Date:** 2026-07-25  
> **Ship gate:** residual R1–R14 addressed in code/docs; staging soak is ops (#149)

## Winner architecture (customer-facing)

The bot no longer treats “trending” as destiny. It builds a **quality-scored universe**:

1. **Hard floors** — Gate-listable, real 24h quote volume (when fetchable)  
2. **Deterministic score** — liquidity, momentum, narrative, memory bias, regime  
3. **Memory + RAG + optional LLM** — soft adjust in shadow; never sole BUY  
4. **Soft** — drop thin non-open names; sort by quality (optional AI score)  
5. **Enforce** — tier caps, min buy score, block toxic new trending adds  
6. **Risk choke-point** — all new BUYs hit WQE gate in RiskManager  

## Package map

```text
services/watchlist_quality/
  memory_bias.py   W1
  scoring.py       W2
  rag_pack.py      AI1
  ai_critic.py     AI2
  engine.py        AI3 orchestrator
  soft.py          W3
  enforce.py       W4
  universe.py      W5 + R2
  runtime.py       soft/enforce on effective list + R12 vol attach
  venue_batch.py   R12
  store.py         R3 tenant files
  metrics.py       R9
  soak.py          AI4
  policy.py        R7
```

## Mode rollout

| Mode | Default | Effect |
|------|---------|--------|
| off | **yes** | no change |
| shadow | opt-in | scores + `/wqe`; list unchanged |
| soft | after soak | filter+sort effective list |
| enforce | after soft soak | tiers + buy gates |

## Stack wiring

| Path | Integration |
|------|-------------|
| `load_effective_watchlist` | soft/enforce transform |
| `RiskManager` approve buy | `buy_allowed` (R1) |
| CMC-only | decision_engine + universe |
| Sensor loop | `get_sensor_watch_coins` (R2) |
| Webhook | observe + risk gate (R6) |
| Manual `/buy` | warn/block (R4) |
| Background runtime | rescore throttle (R5) |
| Telegram | `/wqe`, `/wqe soak` |
| Config | `BotConfig.watchlist_quality_config` defaults off (R11) |

## Tests

- `tests/unit/test_watchlist_quality_*.py` — full module coverage  
- `local_stack_test.sh --unit --telegram` — regression gate  

## Ops

- Runbook: `docs/WQE_STAGING_SOAK.md`  
- Architecture: `docs/WQE_ARCHITECTURE.md`  

## PR to staging checklist (#152)

- [ ] mode default off on merge  
- [ ] local_stack_test green  
- [ ] 48h shadow on Railway staging  
- [ ] soft only after empty-list risk reviewed  

## Soak data logging

Primary: `logs/wqe_events.jsonl` (`wqe_sync`, `wqe_coin`, `wqe_buy_block`, `wqe_soft_apply`).
Human: `watchlist_quality_sync` / `wqe_event` lines in `aria_log.txt`.
Toggle: `watchlist_quality.event_log` (default on when mode ≠ off).
